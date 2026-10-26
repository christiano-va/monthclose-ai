"""PDF text extraction + LLM JSON + history-based category classification."""
import io
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import pdfplumber
from sqlmodel import Session, select

from app.llm import llm_json
from app.mailbox import MailMessage
from app.models import Category, Document
from app.money import to_money
from app.validate import parse_money_field

EXTRACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "document_type",
        "issuer_name",
        "nif",
        "document_number",
        "date",
        "due_date",
        "base",
        "vat_per_rate",
        "total",
        "currency",
        "description",
        "suggested_category",
        "conta_snc",
        "confidence",
        "justification",
        "risk_flags",
    ],
    "properties": {
        "document_type": {"type": "string"},
        "issuer_name": {"type": "string"},
        "nif": {"type": ["string", "null"]},
        "document_number": {"type": "string"},
        "date": {"type": "string"},
        "due_date": {"type": ["string", "null"]},
        "base": {"type": "string"},
        "vat_per_rate": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["rate", "base", "vat"],
                "properties": {
                    "rate": {"type": "string"},
                    "base": {"type": "string"},
                    "vat": {"type": "string"},
                },
            },
        },
        "total": {"type": "string"},
        "currency": {"type": "string"},
        "description": {"type": "string"},
        "suggested_category": {"type": ["string", "null"]},
        "conta_snc": {"type": ["string", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "justification": {"type": "string"},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
    },
}


def pdf_text(data):
    # type: (bytes) -> str
    pages = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            pages.append(text)
    return "\n".join(pages).strip()


def list_categories(session):
    # type: (Session) -> List[Category]
    return session.exec(select(Category).order_by(Category.code)).all()


def lookup_issuer_category(session, nif):
    # type: (Session, Optional[str]) -> Optional[Category]
    if not nif:
        return None
    digits = "".join(ch for ch in str(nif) if ch.isdigit())
    if not digits:
        return None
    statement = (
        select(Document)
        .where(Document.nif == digits)
        .order_by(Document.document_date.desc())
    )
    document = session.exec(statement).first()
    if document is None or document.category_id is None:
        # history sometimes stored NIF with punctuation; try raw
        document = session.exec(
            select(Document)
            .where(Document.nif == str(nif))
            .order_by(Document.document_date.desc())
        ).first()
    if document is None or document.category_id is None:
        return None
    return session.get(Category, document.category_id)


def _category_block(categories):
    # type: (List[Category]) -> str
    lines = []
    for category in categories:
        lines.append(
            "- %s | snc=%s | %s"
            % (category.code, category.snc_account or "", category.description or "")
        )
    return "\n".join(lines)


def extract_pdf_document(pdf_bytes, message, categories, known_category=None):
    # type: (bytes, MailMessage, List[Category], Optional[Category]) -> Dict[str, Any]
    text = pdf_text(pdf_bytes)
    if not text:
        raise RuntimeError("PDF has no extractable text")

    sender_domain = ""
    if "@" in (message.sender or ""):
        sender_domain = message.sender.split("@")[-1].rstrip(">")

    prompt_parts = [
        "Extract the supplier invoice / receipt from this Portuguese PDF.",
        "Issuer is the supplier who issued the document, NOT the customer Nortavia Digital.",
        "Issuer NIF is the supplier tax id (9 digits for Portugal).",
        "document_type examples: fatura, fatura-recibo, recibo, nota_credito, guia, comprovativo.",
        "Money fields as strings with two decimals. Currency EUR unless stated otherwise.",
        "vat_per_rate: one object per VAT rate present (0, 6, 13 or 23).",
        "confidence is 0-1. justification is one line.",
        "risk_flags: list of short strings. Flag payment details that look unusual,",
        "urgency/pressure language, or a sender domain that does not match the issuer.",
        "",
        "Email sender: %s" % message.sender,
        "Email subject: %s" % message.subject,
        "Email date: %s" % (message.date.isoformat() if message.date else ""),
        "Sender domain: %s" % sender_domain,
        "Email body:",
        message.body or "(empty)",
        "",
        "PDF text:",
        text[:12000],
    ]
    if known_category is not None:
        prompt_parts.extend(
            [
                "",
                "Category is already known from history. Set suggested_category to %s"
                % known_category.code,
                "and conta_snc to %s." % (known_category.snc_account or ""),
            ]
        )
    else:
        prompt_parts.extend(
            [
                "",
                "Choose suggested_category and conta_snc from this list only:",
                _category_block(categories),
            ]
        )

    return llm_json("\n".join(prompt_parts), EXTRACT_SCHEMA)


def classify_category(session, extracted):
    # type: (Session, Dict[str, Any]) -> Tuple[Optional[Category], float, str, str]
    """Reuse history category for a known issuer NIF; LLM only if unknown."""
    nif = extracted.get("nif")
    known = lookup_issuer_category(session, nif)
    if known is not None:
        return (
            known,
            0.95,
            "Issuer NIF %s found in history documents; reused category %s"
            % (nif, known.code),
            "history",
        )
    code = extracted.get("suggested_category")
    category = None
    if code:
        category = session.exec(
            select(Category).where(Category.code == code)
        ).first()
    confidence = float(extracted.get("confidence") or 0)
    justification = extracted.get("justification") or "LLM suggested category"
    return category, confidence, justification, "llm"


def primary_vat(extracted):
    # type: (Dict[str, Any]) -> Tuple[Decimal, Decimal]
    """Return (rate, vat_amount) for the VAT line with the largest base."""
    lines = extracted.get("vat_per_rate") or []
    if not lines:
        return to_money(0), to_money(0)
    parsed = []
    for item in lines:
        parsed.append(
            (
                parse_money_field(item.get("base")),
                parse_money_field(item.get("rate")),
                parse_money_field(item.get("vat")),
            )
        )
    parsed.sort(key=lambda row: row[0], reverse=True)
    return parsed[0][1], sum((row[2] for row in parsed), to_money(0))


def guess_tipo(document_type):
    # type: (Optional[str]) -> str
    text = (document_type or "").lower()
    if "credito" in text or "crédito" in text:
        return "gasto"
    if "recibo" in text and "fatura" not in text:
        return "gasto"
    if "guia" in text or "imposto" in text or "at " in text:
        return "estado"
    return "gasto"

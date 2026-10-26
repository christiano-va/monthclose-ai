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
        "document_kind",
        "document_type",
        "issuer_name",
        "nif",
        "document_number",
        "date",
        "due_date",
        "base",
        "vat_per_rate",
        "total",
        "amount",
        "gross",
        "deductions",
        "net",
        "iban",
        "currency",
        "description",
        "suggested_category",
        "conta_snc",
        "confidence",
        "justification",
        "risk_flags",
    ],
    "properties": {
        "document_kind": {
            "type": "string",
            "enum": [
                "invoice",
                "receipt",
                "payslip",
                "tax_guide",
                "payment_proof",
                "payment_notice",
                "bank_statement",
                "payout_report",
                "other",
            ],
        },
        "document_type": {"type": "string"},
        "issuer_name": {"type": "string"},
        "nif": {"type": ["string", "null"]},
        "document_number": {"type": "string"},
        "date": {"type": "string"},
        "due_date": {"type": ["string", "null"]},
        "base": {"type": "string"},
        "amount": {"type": ["string", "null"]},
        "gross": {"type": ["string", "null"]},
        "deductions": {"type": ["string", "null"]},
        "net": {"type": ["string", "null"]},
        "iban": {"type": ["string", "null"]},
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


HISTORY_JUSTIFICATION = "loaded from data/history JSON"


def _norm_nif(value):
    # type: (Optional[str]) -> str
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def lookup_issuer_category(session, nif):
    # type: (Session, Optional[str]) -> Optional[Category]
    """Latest history category for this issuer NIF. Inbox rows are not history."""
    wanted = _norm_nif(nif)
    if not wanted:
        return None
    rows = session.exec(
        select(Document)
        .where(Document.justification == HISTORY_JUSTIFICATION)
        .order_by(Document.document_date.desc(), Document.id.desc())
    ).all()
    for document in rows:
        if document.category_id is None:
            continue
        if _norm_nif(document.nif) == wanted:
            return session.get(Category, document.category_id)
    return None


def _category_block(categories):
    # type: (List[Category]) -> str
    lines = []
    for category in categories:
        lines.append(
            "- %s | snc=%s | %s"
            % (category.code, category.snc_account or "", category.description or "")
        )
    return "\n".join(lines)


def extract_pdf_document(pdf_bytes, message, categories, text=None):
    # type: (bytes, MailMessage, List[Category], Optional[str]) -> Dict[str, Any]
    """Extract document fields. Category is chosen later, not in this call."""
    if text is None:
        text = pdf_text(pdf_bytes)
    if not text:
        raise RuntimeError("PDF has no extractable text")
    # `categories` is accepted so callers can pass the table; classification uses it.
    if categories is None:
        categories = []

    sender_domain = ""
    if "@" in (message.sender or ""):
        sender_domain = message.sender.split("@")[-1].rstrip(">")

    prompt_parts = [
        "Extract the accounting document from this PDF.",
        "The company closing the books is Nortavia Digital, NIF 511588135.",
        "issuer_name and nif are the entity that ISSUED the document.",
        "On a purchase, the issuer is the supplier, not Nortavia.",
        "On a sale or receipt issued by Nortavia, the issuer is Nortavia Digital and nif is 511588135.",
        "Do not swap the customer and the issuer.",
        "document_kind is exactly one of: invoice, receipt, payslip, tax_guide, payment_proof, payment_notice, bank_statement, payout_report, other.",
        "Use invoice for faturas, faturas-recibo, notas de crédito and notas de débito (including VAT-exempt bank fee notes).",
        "Use receipt for payment receipts (recibo / RG), rent receipts and donation receipts.",
        "Use payslip for recibo de vencimento. Use tax_guide for a payment guide (guia) from Segurança Social or Autoridade Tributária.",
        "Use payment_proof for a bank comprovativo de pagamento. Use payment_notice for an aviso de pagamento.",
        "Use bank_statement for an extrato de conta. Use payout_report for a platform payout report. Use other for an expense reimbursement note.",
        "document_type is a short label such as fatura, fatura-recibo, recibo, nota_credito, guia, comprovativo, recibo_vencimento, extrato.",
        "nif is the tax id of the entity that ISSUED the document, copied as printed.",
        "Portuguese issuers have a 9-digit NIF. Foreign issuers have an EU VAT id (for example IE1234567X) or another non-Portuguese id. Do not rewrite a foreign id into 9 digits.",
        "On a tax guide, if the header NIF is zeros, use the taxpayer NIF printed for Nortavia (511588135) when it is on the document.",
        "Money fields are strings with a dot decimal and two places (example 184.50). Copy the figures. Do not invent amounts.",
        "iban is the account the document asks someone to pay, copied as printed, or null if none is printed.",
        "For invoice and receipt: fill base, vat_per_rate and total. vat_per_rate has one object per printed VAT rate (0, 6, 13 or 23).",
        "If an invoice or receipt shows no VAT, use one line with rate \"0\", vat \"0.00\", and base equal to total. amount equals total. gross, deductions and net are null.",
        "On a rent receipt, total is the amount received. When no VAT is charged, base equals that amount; describe any IRS withholding in the description, not as VAT.",
        "For a payslip: gross is the sum of every earning, deductions is the sum of every deduction, net is the take-home pay, and gross - deductions must equal net.",
        "A meal allowance shown both as an earning and as a deduction belongs in both gross and deductions. amount and total equal net. vat_per_rate is an empty list. base may equal gross.",
        "For tax_guide, payment_proof, payment_notice and payout_report: amount is the single sum printed (amount due, amount paid, or total payout). total and base equal amount. vat_per_rate is empty. gross, deductions and net are null.",
        "For bank_statement: amount is the closing balance. vat_per_rate is empty.",
        "For other: amount is the total to pay or reimburse. vat_per_rate is empty.",
        "confidence is 0-1. Lower it when the text is ambiguous or a figure is unclear.",
        "justification is one line.",
        "risk_flags may contain only these names: iban_new, iban_changed, urgency_language, short_payment_deadline, personal_account.",
        "iban_new or iban_changed only when the document says the bank account is new, updated, or different from the usual one. A normal payment IBAN printed on an invoice is not a flag.",
        "urgency_language only for pressure or threat wording (URGENTE, suspensão, last chance). A normal due date, including one forwarded by the accountant, is not urgency.",
        "short_payment_deadline only when the document itself demands payment in a very short time.",
        "personal_account only when payment should go to a private person's account (for example a reimbursement to the manager). Do not flag a supplier invoice just because someone paid it with a personal card.",
        "Never flag a sender domain. This mailbox receives mail forwarded by the accountant and by the owner.",
        "Use an empty list when none of those four situations apply.",
        "Set suggested_category and conta_snc to null. Category is chosen in a separate step.",
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
    return llm_json("\n".join(prompt_parts), EXTRACT_SCHEMA)


CLASSIFY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["category", "confidence", "justification"],
    "properties": {
        "category": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "justification": {"type": "string"},
    },
}


def match_category(categories, raw):
    # type: (List[Category], Optional[str]) -> Optional[Category]
    """Return the Category row when `raw` is exactly a code from the table."""
    text = str(raw or "").strip().strip("\"'`")
    if not text:
        return None
    by_code = {}
    for category in categories:
        by_code[category.code.lower()] = category
    if text.lower() in by_code:
        return by_code[text.lower()]
    first = text.split()[0].strip(".,;:|")
    if first.lower() in by_code:
        return by_code[first.lower()]
    return None


def classify_unknown_issuer(extracted, categories, pdf_text_value, message):
    # type: (Dict[str, Any], List[Category], str, MailMessage) -> Dict[str, Any]
    """Ask the LLM to pick a category code. The table is the only allowed list."""
    prompt = "\n".join(
        [
            "Choose the bookkeeping category for this document.",
            "The company is Nortavia Digital, NIF 511588135.",
            "Answer with one category code copied exactly from the allowed list.",
            "Codes starting with REC_ are income issued by the company.",
            "Other codes are costs, assets, payroll, tax, or other postings.",
            "confidence is 0-1 for this classification. justification is one line.",
            "",
            "document_kind: %s" % (extracted.get("document_kind") or ""),
            "document_type: %s" % (extracted.get("document_type") or ""),
            "issuer_name: %s" % (extracted.get("issuer_name") or ""),
            "nif: %s" % (extracted.get("nif") or ""),
            "document_number: %s" % (extracted.get("document_number") or ""),
            "description: %s" % (extracted.get("description") or ""),
            "email subject: %s" % (message.subject or ""),
            "email sender: %s" % (message.sender or ""),
            "",
            "Allowed categories (code | snc | description):",
            _category_block(categories),
            "",
            "PDF text:",
            (pdf_text_value or "")[:6000],
        ]
    )
    return llm_json(prompt, CLASSIFY_SCHEMA)


def classify_category(session, extracted, categories, pdf_text_value, message):
    # type: (Session, Dict[str, Any], List[Category], str, MailMessage) -> Tuple[Optional[Category], float, str, str, str]
    """Reuse history for a known issuer NIF. Unknown issuers go through the LLM."""
    nif = extracted.get("nif")
    known = lookup_issuer_category(session, nif)
    if known is not None:
        return (
            known,
            0.95,
            "Issuer NIF %s found in history documents; reused category %s"
            % (nif, known.code),
            "history",
            known.code,
        )
    data = classify_unknown_issuer(extracted, categories, pdf_text_value, message)
    raw = str(data.get("category") or "").strip()
    category = match_category(categories, raw)
    confidence = float(data.get("confidence") or 0)
    justification = data.get("justification") or "LLM classified category"
    if category is None:
        justification = (
            "LLM category %r is not in the Category table. %s" % (raw, justification)
        )
    return category, confidence, justification, "llm", raw


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


def guess_tipo(document_type, kind=None):
    # type: (Optional[str], Optional[str]) -> str
    if kind == "tax_guide":
        return "estado"
    if kind == "payslip":
        return "gasto"
    if kind == "payout_report":
        return "receita"
    text = (document_type or "").lower()
    if "credito" in text or "crédito" in text:
        return "gasto"
    if "recibo" in text and "fatura" not in text:
        return "gasto"
    if "guia" in text or "imposto" in text or "at " in text:
        return "estado"
    return "gasto"


PLAIN_EMAIL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "reason",
        "invoice_to_request",
        "counterparty",
        "summary",
        "confidence",
        "justification",
    ],
    "properties": {
        "reason": {"type": "string", "enum": ["marketing", "request", "approval"]},
        "invoice_to_request": {"type": "boolean"},
        "counterparty": {"type": ["string", "null"]},
        "summary": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "justification": {"type": "string"},
    },
}


def classify_plain_email(message):
    # type: (MailMessage) -> Dict[str, Any]
    """Classify an email that has no PDF. Does not invent amounts."""
    prompt = "\n".join(
        [
            "This email has no PDF attachment. Classify why it is in a bookkeeping mailbox.",
            "reason is marketing (newsletter or promotion), request (someone asks for something), or approval (a client approves work).",
            "invoice_to_request is true only when the body says hours were approved or asks the company to issue an invoice.",
            "counterparty is the client or sender organisation, or null.",
            "summary is one line. confidence is 0-1. justification is one line.",
            "Do not invent an amount.",
            "",
            "From: %s" % (message.sender or ""),
            "Subject: %s" % (message.subject or ""),
            "Date: %s" % (message.date.isoformat() if message.date else ""),
            "Body:",
            message.body or "(empty)",
        ]
    )
    return llm_json(prompt, PLAIN_EMAIL_SCHEMA)

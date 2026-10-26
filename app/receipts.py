"""Rules for scanned receipts: restaurant, fuel, parking and stationery.

The model reads the page. These checks decide VAT and the owner task.
A missing company NIF is never filled in.
"""
import re
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from app.models import Category, Document, OpenItem
from app.money import ZERO, to_money
from app.posting import CATEGORY_SUSPECT, TIPO_GASTO, TIPO_SUSPECT, note_value, set_note
from app.settings import company_nif

AWAITING_BANK_LINE = "awaiting bank line"

_FOLD = str.maketrans("áàâãéêíóôõúç", "aaaaeeiooouc")

_RECEIPT_KINDS = (
    ("fuel", ("combustivel", "gasoleo", "gasolina", "diesel", "fuel")),
    ("restaurant", ("restaurante", "restaurant", "almoco", "jantar", "refeicao", "tasca", "prato do dia")),
    ("parking", ("estacionamento", "parking")),
    ("stationery", ("papelaria", "stationery", "material de escritorio")),
)

_CASH_WORDS = ("numerario", "dinheiro", "cash", "em especie")
_PERSONAL_WORDS = (
    "cartao pessoal",
    "personal card",
    "cartao particular",
    "conta pessoal",
)

_FUEL_CATEGORY = "FSE_COMBUSTIVEL"


def _fold(value):
    # type: (str) -> str
    return (value or "").lower().translate(_FOLD)


def receipt_kind_of(text):
    # type: (str) -> Optional[str]
    folded = _fold(text)
    for name, words in _RECEIPT_KINDS:
        for word in words:
            if word in folded:
                return name
    return None


def _nif_tokens(text):
    # type: (str) -> List[str]
    return re.findall(r"(?<!\d)(\d{9})(?!\d)", text or "")


def company_nif_printed(text):
    # type: (str) -> bool
    """True only when the company NIF appears as digits in the document text."""
    try:
        wanted = company_nif()
    except RuntimeError:
        return False
    return wanted in _nif_tokens(text)


def _paid_without_bank_line(text):
    # type: (str) -> Optional[str]
    folded = _fold(text)
    for word in _PERSONAL_WORDS:
        if word in folded:
            return "personal_card"
    for word in _CASH_WORDS:
        if word in folded:
            return "cash"
    return None


def _category(session, code):
    # type: (Session, str) -> Optional[Category]
    return session.exec(select(Category).where(Category.code == code)).first()


def _ensure_bank_task(session, document, how):
    # type: (Session, Document, str) -> OpenItem
    marker = "awaiting_bank_line: %s" % document.id
    rows = session.exec(select(OpenItem).where(OpenItem.document_id == document.id)).all()
    existing = None
    for item in rows:
        if item.notes and marker in item.notes:
            existing = item
            break
    how_label = "cash" if how == "cash" else "a personal card"
    description = (
        "Receipt %s from %s (%s) has no bank line yet. It was paid in %s."
        % (document.document_number, document.entity, to_money(document.total), how_label)
    )
    justification = "Cash or personal-card receipt is stored and waits for a bank line."
    if existing is None:
        existing = OpenItem(
            document_id=document.id,
            period=document.period,
            kind="awaiting_bank_line",
            description=description,
            amount=to_money(abs(document.total or ZERO)),
            status="open",
            notes=marker,
            assignee="owner",
            confidence=0.9,
            justification=justification,
        )
        session.add(existing)
    else:
        existing.description = description
        existing.amount = to_money(abs(document.total or ZERO))
        existing.status = "open"
        existing.assignee = "owner"
        existing.justification = justification
        existing.confidence = 0.9
        session.add(existing)
    return existing


def apply_receipt_rules(session, results, text, note=""):
    # type: (Session, List[Dict[str, Any]], str, str) -> None
    """Adjust scanned receipts. Cash is a waiting status, not an error."""
    payment_text = "\n".join([text or "", note or ""])
    kind_name = receipt_kind_of(text or "")
    how = _paid_without_bank_line(payment_text)
    has_nif = company_nif_printed(text or "")
    changed = False
    for item in results:
        document_id = item.get("document_id")
        if not document_id:
            continue
        document = session.get(Document, document_id)
        if document is None or document.status == "duplicate":
            continue
        notes = document.notes or ""
        stored_kind = note_value(notes, "kind") or ""
        this_kind = kind_name or (receipt_kind_of(notes) if stored_kind == "receipt" else None)
        if stored_kind != "receipt" and not this_kind:
            continue
        if this_kind is None and stored_kind == "receipt":
            this_kind = receipt_kind_of("\n".join([text or "", notes, document.entity or ""]))
        fresh = []  # type: List[str]
        if not has_nif:
            document.deductible_vat = ZERO
            document.notes = set_note(document.notes, "vat_not_deductible", "company NIF is not on the receipt")
            fresh.append("VAT not deductible: company NIF is not on the receipt")
        if this_kind == "fuel":
            fuel = _category(session, _FUEL_CATEGORY)
            if fuel is not None:
                document.category_id = fuel.id
            fresh.append(
                "to review: fuel VAT deductibility is not applied automatically"
            )
        elif this_kind == "restaurant":
            fresh.append(
                "to review: restaurant VAT deductibility is not coded"
            )
        if stored_kind == "receipt" or this_kind:
            document.notes = set_note(document.notes, "kind", "receipt")
            # A receipt is suspect only when a fraud flag was recorded. The
            # classifier's SUSPECT label, a test footer, or an unknown NIF is not one.
            if document.category_id:
                current = session.get(Category, document.category_id)
                flagged = note_value(document.notes, "risk_flags") or ""
                if current is not None and current.code == CATEGORY_SUSPECT and not flagged:
                    document.category_id = None
                    if document.tipo == TIPO_SUSPECT:
                        document.tipo = TIPO_GASTO
        if this_kind:
            document.notes = set_note(document.notes, "receipt_kind", this_kind)
        if how:
            document.payment_method = how
            document.status = AWAITING_BANK_LINE
            document.notes = set_note(document.notes, "awaiting_bank_line", how)
            fresh.append(
                "no bank line yet: paid in cash or with a personal card"
            )
            _ensure_bank_task(session, document, how)
        elif this_kind in ("fuel", "restaurant") or not has_nif:
            document.status = "review"
        owned = (
            "VAT not deductible:",
            "to review:",
            "no bank line yet:",
        )
        kept = []  # type: List[str]
        for piece in (item.get("reason") or "").split("; "):
            piece = piece.strip()
            if piece and not piece.startswith(owned):
                kept.append(piece)
        reasons = kept + fresh
        still_suspect = False
        if document.category_id:
            current = session.get(Category, document.category_id)
            still_suspect = current is not None and current.code == CATEGORY_SUSPECT
        if still_suspect and document.status == "validated":
            document.status = "review"
        if not reasons and (document.status in ("review", "error") or still_suspect):
            reasons = ["review required: the triggering rule was not recorded"]
        document.notes = set_note(
            document.notes,
            "reasons",
            "; ".join(reasons).replace(" | ", " / "),
        )
        session.add(document)
        item["status"] = document.status
        item["reason"] = "; ".join(reasons)
        changed = True
    if changed:
        session.commit()

"""Process one .eml: extract each PDF, classify, validate, store Document."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from datetime import timedelta
from decimal import InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db import ROOT, engine, init_db
from app.extract import (
    classify_category,
    classify_plain_email,
    extract_pdf_document,
    list_categories,
    pdf_text,
)
from app.mailbox import Attachment, LocalEmlMailbox, MailMessage
from app.models import Category, Document, Link, OpenItem, Period
from app.money import ZERO, money_equal, to_money
from app.posting import (
    CATEGORY_SETTLEMENT,
    CATEGORY_SUSPECT,
    OWNER_TASK_KINDS,
    TIPO_SUSPECT,
    deductible_vat,
    ensure_category,
    ensure_special_categories,
    excluded_from_linking,
    find_referenced_invoices,
    group_by_nature,
    is_company_nif,
    is_final_consumer,
    is_fixed_asset,
    is_meal_topup,
    is_settlement,
    is_tax_or_social,
    main_line,
    norm_nif,
    party_label,
    primary_rate,
    resolve_counterparty,
    resolve_direction,
    resolve_tipo,
    settlement_account,
    should_split_lines,
    sum_lines,
    task_assignee,
    tax_category_code,
    asset_category_code,
    consumer_sale_category,
)
from app.statements import import_statement_text
from app.validate import (
    classify_nif,
    find_ibans,
    align_line_rounding,
    line_items,
    normalize_document_kind,
    normalize_iban,
    parse_iso_date,
    parse_money_field,
    total_payable_amount,
    resolve_risk_flags,
    validate_extraction,
)

logger = logging.getLogger("app.process")

# Human review when the AI confidence is below this. History reuse is 0.95.
LOW_CONFIDENCE_THRESHOLD = 0.8

_HOURS_RE = re.compile(r"(\d{1,4})\s+horas", re.IGNORECASE)
_BOOKING_RE = re.compile(r"\b([A-Z]{2,}\d[A-Z0-9]{2,})\b")
ATTACH_DIR = ROOT / "data" / "attachments"


def _get_or_create_period(session, month):
    # type: (Session, str) -> Period
    period = session.exec(select(Period).where(Period.month == month)).first()
    if period is None:
        period = Period(month=month, status="open")
        session.add(period)
        session.commit()
        session.refresh(period)
    return period


def _stored_nif(nif):
    # type: (Optional[str]) -> Optional[str]
    kind, norm = classify_nif(nif)
    if kind == "absent":
        return None
    return norm or None


def _one_line(text):
    # type: (Optional[str]) -> str
    return " ".join(str(text or "").split())


def _pdf_attachments(message):
    # type: (MailMessage) -> List[Attachment]
    found = []
    for att in message.attachments:
        name = att.filename or ""
        if att.content_type == "application/pdf" or name.lower().endswith(".pdf"):
            found.append(att)
    return found


def _csv_attachment(message, pdf_name):
    # type: (MailMessage, str) -> Optional[Attachment]
    stem = Path(pdf_name or "").stem.lower()
    csvs = []  # type: List[Attachment]
    for att in message.attachments:
        name = (att.filename or "").lower()
        if name.endswith(".csv") or att.content_type in ("text/csv", "application/csv"):
            csvs.append(att)
    for att in csvs:
        if Path(att.filename or "").stem.lower() == stem:
            return att
    if len(csvs) == 1:
        return csvs[0]
    return None


def _decode_text(data):
    # type: (bytes) -> str
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def attachment_source_id(message, attachment, eml_path):
    # type: (MailMessage, Attachment, Path) -> str
    """Idempotency key: Message-ID plus SHA-256 of the attachment bytes."""
    message_id = (message.message_id or "").strip()
    if not message_id:
        message_id = "missing-message-id:%s" % Path(eml_path).name
    digest = hashlib.sha256(attachment.data).hexdigest()
    return "%s:%s" % (message_id, digest)


def email_source_id(message, eml_path):
    # type: (MailMessage, Path) -> str
    message_id = (message.message_id or "").strip()
    if not message_id:
        message_id = "missing-message-id:%s" % Path(eml_path).name
    return "email:%s" % message_id


def _legacy_source_id(eml_path, attachment):
    # type: (Path, Attachment) -> str
    name = attachment.filename or "attachment.pdf"
    return "inbox:%s:%s" % (Path(eml_path).name, name)


def _review_reason(validation, confidence, flags, category, raw_code):
    # type: (Dict[str, Any], float, List[str], Optional[Any], str) -> Any
    """Return (status, reason). A foreign issuer is a review note, not a checksum failure."""
    chunks = []  # type: List[str]
    if not validation.get("ok"):
        errors = validation.get("errors") or ["validation failed"]
        chunks.append("validation failure: " + "; ".join(errors))
    if validation.get("foreign_note"):
        chunks.append(validation["foreign_note"])
    if confidence < LOW_CONFIDENCE_THRESHOLD:
        chunks.append("low confidence: %.2f" % confidence)
    if flags:
        chunks.append("risk_flag: " + "; ".join(flags))
    if category is None:
        if raw_code:
            chunks.append("unknown category: %s" % raw_code)
        else:
            chunks.append("unknown category")
    if not chunks:
        return "validated", ""
    return "review", "; ".join(chunks)


def _stored_reason(notes, status):
    # type: (Optional[str], str) -> str
    if status not in ("review", "error", "no_document", "bank_statement"):
        return ""
    if not notes:
        return status
    for part in notes.split(" | "):
        if part.startswith("reasons: "):
            return part[len("reasons: ") :]
    return notes


def _item(
    email,
    attachment,
    status,
    reason,
    document_id=None,
    already_existed=False,
    source_id=None,
    reprocessed=False,
    extra=None,
):
    # type: (str, str, str, str, Optional[int], bool, Optional[str], bool, Optional[Dict[str, Any]]) -> Dict[str, Any]
    row = {
        "email": email,
        "attachment": attachment or "",
        "status": status,
        "reason": reason or "",
        "document_id": document_id,
        "already_existed": already_existed,
        "reprocessed": reprocessed,
        "source_id": source_id,
    }
    if extra:
        row.update(extra)
    return row


def _from_existing(document, email, attachment):
    # type: (Document, str, str) -> Dict[str, Any]
    status = document.status or "review"
    return _item(
        email,
        attachment,
        status,
        _stored_reason(document.notes, status),
        document_id=document.id,
        already_existed=True,
        source_id=document.source_id,
    )


def _find_stored(session, source_id, legacy_id):
    # type: (Session, str, str) -> Optional[Document]
    found = session.exec(
        select(Document).where(Document.source_id == source_id)
    ).first()
    if found is not None:
        return found
    legacy = session.exec(
        select(Document).where(Document.source_id == legacy_id)
    ).first()
    if legacy is None:
        return None
    legacy.source_id = source_id
    session.add(legacy)
    session.commit()
    session.refresh(legacy)
    return legacy


def _drop_document(session, document):
    # type: (Session, Document) -> None
    links = session.exec(select(Link).where(Link.document_id == document.id)).all()
    for link in links:
        session.delete(link)
    items = session.exec(
        select(OpenItem).where(OpenItem.document_id == document.id)
    ).all()
    for item in items:
        session.delete(item)
    session.delete(document)
    session.commit()


def _rel_path(path):
    # type: (Path) -> str
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _is_bank_statement(kind, filename, text):
    # type: (str, str, str) -> bool
    name = (filename or "").lower()
    sample = (text or "")[:2000].lower()
    looks = "extrato" in name and (
        "saldo" in sample or "data mov" in sample or "extrato" in sample
    )
    if looks:
        return True
    if kind == "bank_statement" and (
        "extrato" in name or "saldo inicial" in sample or "extrato de conta" in sample
    ):
        return True
    return False


def _issuer_history(session, nif, source_id):
    # type: (Session, Optional[str], str) -> Tuple[bool, List[str]]
    key = _stored_nif(nif) or ""
    if not key:
        return False, []
    known = False
    ibans = []  # type: List[str]
    rows = session.exec(select(Document)).all()
    for document in rows:
        if document.source_id == source_id:
            continue
        if (_stored_nif(document.nif) or "") != key:
            continue
        known = True
        for iban in find_ibans(document.notes or ""):
            if iban not in ibans:
                ibans.append(iban)
    return known, ibans


def _payment_iban(extracted, pdf_text_value):
    # type: (Dict[str, Any], str) -> str
    candidate = normalize_iban(extracted.get("iban"))
    if candidate:
        return candidate
    found = find_ibans(pdf_text_value)
    if found:
        return found[0]
    return ""


def _apply_document(session, existing, fields):
    # type: (Session, Optional[Document], Dict[str, Any]) -> Tuple[Document, bool]
    """Insert or update. Returns (document, skipped_existing)."""
    if existing is None:
        document = Document(**fields)
        session.add(document)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            found = session.exec(
                select(Document).where(Document.source_id == fields.get("source_id"))
            ).first()
            if found is None:
                raise
            return found, True
        session.refresh(document)
        return document, False

    for key, value in fields.items():
        setattr(existing, key, value)
    session.add(existing)
    session.commit()
    session.refresh(existing)
    return existing, False


def _import_bank_pdf(session, message, attachment, path, source_id, existing, text):
    # type: (Session, MailMessage, Attachment, Path, str, Optional[Document], str) -> Dict[str, Any]
    filename = attachment.filename or "attachment.pdf"
    csv_att = _csv_attachment(message, filename)
    if csv_att is None:
        return _item(
            path.name,
            filename,
            "error",
            "bank statement PDF was not imported: no CSV attachment in the email",
            document_id=existing.id if existing is not None else None,
            source_id=source_id,
        )
    imported = import_statement_text(
        session,
        _decode_text(csv_att.data),
        justification="imported from bank statement CSV attached to the email",
    )
    kept = imported["created"] + imported["skipped"]
    if kept <= 0:
        return _item(
            path.name,
            filename,
            "error",
            "bank statement importer read no lines from %s" % (csv_att.filename or "csv"),
            document_id=existing.id if existing is not None else None,
            source_id=source_id,
        )
    if existing is not None:
        _drop_document(session, existing)
    reason = "imported %s (created %d, already stored %d)" % (
        csv_att.filename or "statement.csv",
        imported["created"],
        imported["skipped"],
    )
    return _item(
        path.name,
        filename,
        "bank_statement",
        reason,
        source_id=source_id,
        reprocessed=existing is not None,
        extra={"statement": imported, "pdf_text_chars": len(text or "")},
    )


def _plain_email_fallback(message):
    # type: (MailMessage) -> Dict[str, Any]
    text = "%s\n%s" % (message.subject or "", message.body or "")
    lowered = text.lower()
    invoice = ("hora" in lowered and "aprov" in lowered) or (
        "enviar a fatura" in lowered
    )
    if invoice or "aprov" in lowered:
        reason = "approval"
    elif any(
        word in lowered
        for word in ("newsletter", "saiba mais", "pacote", "promo", "oferta", "gbps")
    ):
        reason = "marketing"
    else:
        reason = "request"
    return {
        "reason": reason,
        "invoice_to_request": invoice,
        "counterparty": None,
        "summary": _one_line(message.subject),
        "confidence": 0.6,
        "justification": "Classified from the email text without a model result.",
    }


def _find_open_item(session, source_id):
    # type: (Session, str) -> Optional[OpenItem]
    marker = "source_id: %s" % source_id
    rows = session.exec(
        select(OpenItem).where(OpenItem.kind == "invoice_to_request")
    ).all()
    for item in rows:
        if item.notes and marker in item.notes:
            return item
    return None


def _sender_name(sender):
    # type: (str) -> str
    text = sender or ""
    if "<" in text:
        text = text.split("<", 1)[0]
    return text.replace('"', "").strip() or "the client"


def process_email_without_pdf(session, message, eml_path, reprocess=False):
    # type: (Session, MailMessage, Path, bool) -> Dict[str, Any]
    """An email with no PDF is not an error. Hours approved become an open item."""
    path = Path(eml_path)
    source_id = email_source_id(message, path)
    try:
        classified = classify_plain_email(message)
    except Exception:
        logger.exception("plain-email classification failed for %s", path.name)
        classified = _plain_email_fallback(message)

    body = "%s\n%s" % (message.subject or "", message.body or "")
    lowered = body.lower()
    asks_invoice = ("hora" in lowered and "aprov" in lowered) or (
        "enviar a fatura" in lowered
    )
    if asks_invoice:
        model_already = bool(classified.get("invoice_to_request"))
        classified["invoice_to_request"] = True
        if classified.get("reason") == "marketing":
            classified["reason"] = "approval"
        if not model_already:
            classified["justification"] = _one_line(
                "%s Email states that hours were approved and an invoice should be issued."
                % (classified.get("justification") or "")
            )

    reason_code = str(classified.get("reason") or "request")
    if reason_code not in ("marketing", "request", "approval"):
        reason_code = "request"
    confidence = float(classified.get("confidence") or 0)
    justification = _one_line(classified.get("justification") or "Email has no PDF.")
    hours_match = _HOURS_RE.search(body)
    hours = hours_match.group(1) if hours_match else ""
    counterparty = _one_line(classified.get("counterparty")) or _sender_name(
        message.sender
    )

    open_item_id = None
    updated_open_item = False
    if classified.get("invoice_to_request"):
        if message.date is not None:
            period = message.date.strftime("%Y-%m")
            due = message.date.date()
        else:
            period = "1970-01"
            due = None
        if due is not None and "30 dia" in lowered:
            due = due + timedelta(days=30)
        elif due is not None:
            due = None
        draft = _draft_invoice_email(counterparty, hours, period)
        if hours:
            description = (
                "Owner: approve the draft email asking the accountant to issue "
                "the sales invoice to %s for %s hours" % (counterparty, hours)
            )
        else:
            description = (
                "Owner: approve the draft email asking the accountant to issue "
                "the sales invoice to %s" % counterparty
            )
        notes = " | ".join(
            [
                "source_id: %s" % source_id,
                "email: %s" % path.name,
                "reason: %s" % reason_code,
                "summary: %s" % _one_line(classified.get("summary") or message.subject),
            ]
        )
        existing = _find_open_item(session, source_id)
        if existing is None:
            _get_or_create_period(session, period)
            item = OpenItem(
                document_id=None,
                period=period,
                kind="invoice_to_request",
                description=description,
                amount=ZERO,
                due_date=due,
                status="open",
                notes=notes,
                assignee=task_assignee("invoice_to_request"),
                draft_email=draft,
                confidence=confidence,
                justification=justification or "Hours approved; invoice still to issue.",
            )
            session.add(item)
            session.commit()
            session.refresh(item)
            open_item_id = item.id
        elif reprocess:
            _get_or_create_period(session, period)
            existing.period = period
            existing.description = description
            existing.amount = ZERO
            existing.due_date = due
            existing.status = "open"
            existing.notes = notes
            existing.assignee = task_assignee("invoice_to_request")
            existing.draft_email = draft
            existing.confidence = confidence
            existing.justification = justification or existing.justification
            session.add(existing)
            session.commit()
            session.refresh(existing)
            open_item_id = existing.id
            updated_open_item = True
        else:
            open_item_id = existing.id

    reason = reason_code
    if classified.get("invoice_to_request"):
        detail = "invoice_to_request"
        if hours:
            detail = "%s (%s hours)" % (detail, hours)
        reason = "%s; %s" % (reason_code, detail)
    return _item(
        path.name,
        "",
        "no_document",
        reason,
        source_id=source_id,
        reprocessed=updated_open_item,
        extra={
            "open_item_id": open_item_id,
            "confidence": confidence,
            "justification": justification,
        },
    )


def _draft_invoice_email(counterparty, hours, period):
    # type: (str, str, str) -> str
    """Draft only. The owner approves it; nothing is sent to the accountant."""
    subject = "Pedido de emissão de fatura — %s — %s" % (counterparty, period)
    if hours:
        detail = "O cliente %s aprovou %s horas de %s." % (counterparty, hours, period)
    else:
        detail = "Há uma fatura de venda por emitir a %s (%s)." % (counterparty, period)
    body = (
        "Bom dia,\n\n%s "
        "Peço a emissão da fatura, com o prazo de pagamento habitual.\n\n"
        "Obrigado."
    ) % detail
    return "To: accountant\nSubject: %s\n\n%s" % (subject, body)


def _save_attachment(data, original_filename):
    # type: (bytes, str) -> Path
    ATTACH_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()[:16]
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", original_filename or "attachment.pdf")
    cleaned = cleaned.strip("._") or "attachment.pdf"
    path = ATTACH_DIR / ("%s_%s" % (digest, cleaned))
    if not path.exists():
        path.write_bytes(data)
    return path


def _lines_json(lines):
    # type: (list) -> str
    payload = []
    for line in lines:
        payload.append(
            {
                "description": line.get("description") or "",
                "base": str(line["base"]),
                "vat_rate": str(line["vat_rate"]),
                "vat": str(line["vat"]),
                "total": str(line["total"]),
                "nature": line.get("nature") or "supply",
            }
        )
    return json.dumps(payload, ensure_ascii=False)


def _category_by_code(session, code):
    # type: (Session, Optional[str]) -> Optional[Category]
    if not code:
        return None
    return session.exec(select(Category).where(Category.code == code)).first()


def _correct_kind(kind, extracted, text):
    # type: (str, Dict[str, Any], str) -> str
    """Keep a fatura-recibo as the invoice, not as a settlement receipt."""
    head = (text or "")[:600].upper()
    if "FATURA-RECIBO" in head or "FATURA RECIBO" in head:
        extracted["settles_invoice"] = False
        extracted["referenced_invoice"] = None
        return "invoice"
    if "RECIBO DE VENCIMENTO" in head:
        return "payslip"
    if "AVISO DE PAGAMENTO" in head:
        return "payment_notice"
    if "COMPROVATIVO DE PAGAMENTO" in head:
        return "payment_proof"
    if "PAYOUT REPORT" in head:
        return "payout_report"
    if "GUIA DE PAGAMENTO" in head or "DOCUMENTO DE PAGAMENTO" in head:
        return "tax_guide"
    if "NOTA DE DESPESAS" in head or "PEDIDO DE REEMBOLSO" in head:
        return "other"
    return kind


def _retag_meal_card(lines, text):
    # type: (list, str) -> list
    """A top-up bundled with a fee must not be booked as the fee."""
    blob = (text or "").lower()
    if "carreg" not in blob or "comiss" not in blob:
        return lines
    for line in lines:
        desc = (line.get("description") or "").lower()
        if "comiss" in desc:
            line["nature"] = "fee"
        elif "carreg" in desc or abs(line["total"]) >= to_money("20"):
            line["nature"] = "topup"
    return lines


def _is_credit_note(extracted, text):
    # type: (Dict[str, Any], str) -> bool
    blob = " ".join(
        [
            str(extracted.get("document_type") or ""),
            str(extracted.get("document_number") or ""),
            (text or "")[:800],
        ]
    ).lower()
    return (
        "nota de crédito" in blob
        or "nota de credito" in blob
        or "credit note" in blob
    )


def _negate(value):
    # type: (Any) -> Any
    amount = to_money(value)
    if amount > ZERO:
        return to_money(-amount)
    return amount


def _select_category(
    session, extracted, text, message, kind, direction, counterparty_nif, line_text, nature
):
    # type: (Session, Dict[str, Any], str, MailMessage, str, str, Optional[str], str, str) -> Tuple[Optional[Category], float, str, str, str]
    """History memory uses the counterparty NIF. Rules cover the systematic cases."""
    if kind == "payslip":
        category = _category_by_code(session, "PESSOAL_REMUNERACOES")
        code = category.code if category is not None else "PESSOAL_REMUNERACOES"
        return (
            category,
            0.95,
            "Payslip of the worker; category PESSOAL_REMUNERACOES.",
            "rule",
            code,
        )

    if is_tax_or_social(kind, text, extracted.get("counterparty_name") or ""):
        code = tax_category_code(text, extracted.get("counterparty_name") or "")
        if code:
            category = _category_by_code(session, code)
            return (
                category,
                0.95,
                "Tax or social-security document; category %s." % code,
                "rule",
                code,
            )
        return (
            None,
            0.7,
            "IRS withholdings have no category in the ledger.",
            "rule",
            "UNMAPPED",
        )

    if nature != "fee" and (nature == "topup" or is_meal_topup(line_text)):
        category = _category_by_code(session, "PESSOAL_REMUNERACOES")
        code = category.code if category is not None else "PESSOAL_REMUNERACOES"
        return (
            category,
            0.9,
            "Main item is a meal-card top-up, not the smaller loading fee.",
            "rule",
            code,
        )

    if direction == "purchase" and is_fixed_asset(line_text):
        code = asset_category_code(line_text)
        category = _category_by_code(session, code)
        return (
            category,
            0.9,
            "Main line is a fixed asset (%s), not a consumable." % code,
            "rule",
            code,
        )

    if direction == "sale" and is_final_consumer(counterparty_nif):
        code = consumer_sale_category(line_text)
        if code:
            category = _category_by_code(session, code)
            return (
                category,
                0.92,
                "Final-consumer NIF is shared; the main line selects %s." % code,
                "rule",
                code,
            )

    extracted["main_line"] = line_text
    extracted["direction"] = direction
    if line_text:
        extracted["description"] = line_text
    return classify_category(session, extracted, list_categories(session), text, message)


def _sale_category_ok(category, kind):
    # type: (Optional[Category], str) -> bool
    if category is None or kind == "payslip":
        return True
    code = category.code
    if code.startswith("REC_"):
        return True
    if code.startswith("PESSOAL_") or code.startswith("ESTADO_") or code.startswith("EQUIPAMENTO_"):
        return True
    if code in (CATEGORY_SETTLEMENT, CATEGORY_SUSPECT):
        return True
    return False


def _snapshot(session, document):
    # type: (Session, Document) -> Dict[str, str]
    old_code = ""
    if document.category_id:
        row = session.get(Category, document.category_id)
        old_code = row.code if row is not None else ""
    return {
        "category": old_code,
        "tipo": document.tipo or "",
        "counterparty": party_label(document.entity, document.nif),
    }


def _changes(document_id, old, new_category, new_tipo, new_party):
    # type: (Optional[int], Optional[Dict[str, str]], str, str, str) -> List[Dict[str, Any]]
    if old is None:
        return []
    rows = []
    pairs = (
        ("category", old["category"], new_category),
        ("tipo", old["tipo"], new_tipo),
        ("counterparty", old["counterparty"], new_party),
    )
    for field, previous, current in pairs:
        if previous != current:
            rows.append(
                {
                    "id": document_id,
                    "field": field,
                    "old": previous,
                    "new": current,
                }
            )
    return rows


def _find_sale(session, description, amount):
    # type: (Session, str, Any) -> Optional[Document]
    tokens = re.findall(r"[A-Za-zÀ-ÿ]{4,}", description or "")
    rows = session.exec(select(Document).where(Document.tipo == "receita")).all()
    inbox = []
    for document in rows:
        if document.justification == "loaded from data/history JSON":
            continue
        inbox.append(document)
    for document in inbox:
        entity = (document.entity or "").lower()
        for token in tokens:
            if token.lower() in entity:
                return document
    if amount is not None:
        for document in inbox:
            if money_equal(document.total, amount):
                return document
    return None


def _record_missing_sale(
    session, email_name, source_id, period, description, amount, reference, document_id
):
    # type: (Session, str, str, str, str, Any, str, Optional[int]) -> Optional[int]
    marker = "missing:%s:%s" % (source_id, reference or description[:40])
    rows = session.exec(
        select(OpenItem).where(OpenItem.kind == "missing_document")
    ).all()
    notes = " | ".join(
        [
            marker,
            "email: %s" % email_name,
            "reference: %s" % (reference or "-"),
        ]
    )
    label = reference or _one_line(description) or "a stay"
    text = (
        "Owner: missing sales invoice for %s (%s). "
        "Obtain the document; nothing is sent to the accountant automatically."
        % (label, amount)
    )
    for item in rows:
        if item.notes and marker in item.notes:
            item.assignee = "owner"
            item.description = text
            item.amount = to_money(amount)
            item.period = period
            item.document_id = document_id
            item.notes = notes
            item.status = "open"
            session.add(item)
            session.commit()
            return item.id
    _get_or_create_period(session, period)
    item = OpenItem(
        document_id=document_id,
        period=period,
        kind="missing_document",
        description=text,
        amount=to_money(amount),
        due_date=None,
        status="open",
        notes=notes,
        assignee="owner",
        confidence=0.85,
        justification="Payout lists a stay with no sales invoice in the mailbox.",
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item.id


def _missing_sales(session, email_name, source_id, period, lines, document_id):
    # type: (Session, str, str, str, list, Optional[int]) -> List[str]
    """Bookings on a payout that have no sales invoice become owner tasks."""
    matched = []
    for line in lines:
        if (line.get("nature") or "") == "fee" or line["total"] <= ZERO:
            continue
        description = line.get("description") or ""
        found = _BOOKING_RE.search(description.upper())
        reference = found.group(1) if found else ""
        sale = _find_sale(session, description, line["total"])
        if sale is not None:
            if sale.document_number and sale.document_number not in matched:
                matched.append(sale.document_number)
            continue
        _record_missing_sale(
            session,
            email_name,
            source_id,
            period,
            description,
            line["total"],
            reference,
            document_id,
        )
    return matched


def _tax_counterparty(kind, text, direction, name, nif):
    # type: (str, str, str, str, Optional[str]) -> Tuple[str, Optional[str], str]
    """Bank proofs and tax guides name the authority, not the company or the bank."""
    blob = (text or "").lower()
    if kind == "payment_proof":
        if "segurança social" in blob or "seguranca social" in blob:
            return "Segurança Social", None, "purchase"
        if (
            "autoridade tribut" in blob
            or "estado (at" in blob
            or "reten" in blob
            or "pagamento por conta" in blob
        ):
            return "Autoridade Tributária", None, "purchase"
    if kind == "tax_guide":
        if "segurança social" in blob or "seguranca social" in blob:
            return "Segurança Social", None, "purchase"
        if "autoridade tribut" in blob or "pagamento por conta" in blob or "irs" in blob or "irc" in blob:
            return "Autoridade Tributária", None, "purchase"
    if kind == "payslip" and (is_company_nif(nif) or "nortavia" in (name or "").lower()):
        return name, nif, direction
    return name, nif, direction


def ensure_owner_tasks(session):
    # type: (Session) -> None
    """missing_document and invoice_to_request are tasks for the owner."""
    rows = session.exec(select(OpenItem)).all()
    changed = False
    for item in rows:
        if item.kind in OWNER_TASK_KINDS and item.assignee != "owner":
            item.assignee = "owner"
            session.add(item)
            changed = True
    if changed:
        session.commit()


def process_attachment(
    session, message, attachment, eml_path, categories, reprocess=False
):
    # type: (Session, MailMessage, Attachment, Path, list, bool) -> List[Dict[str, Any]]
    """Extract, classify, validate, and store one PDF. May store one row per line group."""
    path = Path(eml_path)
    filename = attachment.filename or "attachment.pdf"
    source_id = attachment_source_id(message, attachment, path)
    existing = _find_stored(session, source_id, _legacy_source_id(path, attachment))
    if existing is not None and not reprocess:
        return [_from_existing(existing, path.name, filename)]

    text = pdf_text(attachment.data)
    extracted = extract_pdf_document(attachment.data, message, categories, text=text)
    kind = normalize_document_kind(
        extracted.get("document_kind") or extracted.get("document_type")
    )
    kind = _correct_kind(kind, extracted, text)
    extracted["document_kind"] = kind

    if _is_bank_statement(kind, filename, text):
        return [
            _import_bank_pdf(
                session, message, attachment, path, source_id, existing, text
            )
        ]

    issuer_nif = extracted.get("issuer_nif") or extracted.get("nif")
    extracted["issuer_nif"] = issuer_nif
    direction = resolve_direction(issuer_nif)
    counterparty_name, counterparty_nif = resolve_counterparty(
        direction,
        extracted.get("issuer_name") or "",
        issuer_nif,
        extracted.get("recipient_name") or "",
        extracted.get("recipient_nif"),
        extracted.get("payee_name") or "",
        extracted.get("payee_nif"),
    )
    counterparty_name, counterparty_nif, direction = _tax_counterparty(
        kind, text, direction, counterparty_name, counterparty_nif
    )
    extracted["counterparty_name"] = counterparty_name
    extracted["counterparty_nif"] = counterparty_nif or ""
    extracted["nif"] = counterparty_nif
    extracted["direction"] = direction

    lines = _retag_meal_card(line_items(extracted), text)
    try:
        lines = align_line_rounding(lines, total_payable_amount(extracted))
    except (InvalidOperation, ValueError):
        pass
    extracted["lines"] = [
        {
            "description": line["description"],
            "base": str(line["base"]),
            "vat_rate": str(line["vat_rate"]),
            "vat": str(line["vat"]),
            "total": str(line["total"]),
            "nature": line["nature"],
        }
        for line in lines
    ]
    validation = validate_extraction(extracted)
    if not (direction == "purchase" and kind == "invoice"):
        validation["foreign_note"] = None

    try:
        doc_date = parse_iso_date(validation.get("document_date") or extracted.get("date"))
    except (TypeError, ValueError):
        doc_date = None
    try:
        due_date = parse_iso_date(validation.get("due_date") or extracted.get("due_date"))
    except (TypeError, ValueError):
        due_date = None

    payment_iban = _payment_iban(extracted, text)
    issuer_known, previous_ibans = _issuer_history(session, issuer_nif, source_id)
    flag_text = "\n".join([message.subject or "", message.body or "", text or ""])
    flags = resolve_risk_flags(
        extracted.get("risk_flags"),
        flag_text,
        previous_ibans,
        issuer_known,
        kind,
        doc_date,
        due_date,
        payment_iban,
    )
    suspect = bool(flags)

    if doc_date is None and existing is not None and existing.document_date is not None:
        doc_date = existing.document_date
    if doc_date is None:
        status, reason = _review_reason(validation, float(extracted.get("confidence") or 0), flags, None, "")
        extra = "validation failure: cannot store document without a date"
        reason = (reason + "; " + extra).strip("; ")
        return [
            _item(
                path.name,
                filename,
                "review" if status == "validated" else status,
                reason,
                document_id=existing.id if existing is not None else None,
                source_id=source_id,
                extra={"document_kind": kind, "validation": validation},
            )
        ]

    referenced = ""
    settles = is_settlement(
        kind,
        bool(extracted.get("settles_invoice")),
        str(extracted.get("referenced_invoice") or ""),
        text,
    )
    if settles:
        referenced = find_referenced_invoices(
            extracted.get("referenced_invoice"),
            "\n".join([text or "", message.subject or "", message.body or ""]),
            str(extracted.get("document_number") or ""),
        )

    credit = _is_credit_note(extracted, text)
    if suspect or settles or not should_split_lines(lines):
        base = parse_money_field(validation.get("base"))
        vat_amount = parse_money_field(validation.get("vat"))
        total = parse_money_field(validation.get("total"))
        slices = [
            {
                "nature": "all",
                "lines": lines,
                "base": base,
                "vat": vat_amount,
                "total": total,
                "source_id": source_id,
            }
        ]
    else:
        grouped = group_by_nature(lines)
        slices = []
        for index, (nature, group) in enumerate(grouped):
            base, vat_amount, total = sum_lines(group)
            sid = source_id if index == 0 else "%s:nature:%s" % (source_id, nature)
            slices.append(
                {
                    "nature": nature,
                    "lines": group,
                    "base": base,
                    "vat": vat_amount,
                    "total": total,
                    "source_id": sid,
                }
            )

    saved = _save_attachment(attachment.data, filename)
    period_month = doc_date.strftime("%Y-%m")
    _get_or_create_period(session, period_month)
    ensure_special_categories(session)
    suspect_category = _category_by_code(session, CATEGORY_SUSPECT)
    settlement_category = _category_by_code(session, CATEGORY_SETTLEMENT)
    extraction_confidence = float(extracted.get("confidence") or 0)
    results = []
    payout_document_id = None

    for piece in slices:
        piece_lines = piece["lines"]
        focus = main_line(piece_lines)
        line_text = ""
        if focus is not None:
            line_text = focus.get("description") or ""
        if not line_text:
            line_text = extracted.get("description") or ""
        nature = piece["nature"]
        if nature == "all" and focus is not None:
            nature = focus.get("nature") or "supply"

        sid = piece["source_id"]
        current = existing if sid == source_id else _find_stored(session, sid, sid)
        old = _snapshot(session, current) if current is not None else None

        if suspect:
            category = suspect_category
            raw_code = CATEGORY_SUSPECT
            cat_confidence = extraction_confidence
            cat_justification = "Risk flags %s; no accounting entry, kept for review." % (
                ", ".join(flags)
            )
            cat_source = "rule"
            tipo = TIPO_SUSPECT
        elif settles:
            category = settlement_category
            raw_code = CATEGORY_SETTLEMENT
            account = settlement_account(session, kind, direction, counterparty_nif)
            cat_confidence = 0.95
            cat_justification = (
                "Settles invoice %s; account %s; not booked again as revenue or expense."
                % (referenced or "already issued", account)
            )
            cat_source = "rule"
            tipo = resolve_tipo(direction, kind, CATEGORY_SETTLEMENT, True, False, False)
        else:
            account = ""
            category, cat_confidence, cat_justification, cat_source, raw_code = _select_category(
                session,
                extracted,
                text,
                message,
                kind,
                direction,
                counterparty_nif,
                line_text,
                nature,
            )
            if nature == "fee" and (
                category is None or category.code.startswith("PESSOAL_")
            ):
                replacement = _category_by_code(session, "FSE_COMISSOES_PLATAFORMA")
                if replacement is not None:
                    category = replacement
                    raw_code = replacement.code
                    cat_justification = (
                        "Loading commission booked apart from the meal-card top-up."
                    )
                    cat_source = "rule"
                    cat_confidence = 0.85
            if direction == "sale" and not _sale_category_ok(category, kind):
                raw_code = category.code if category is not None else raw_code
                cat_justification = (
                    "Sale was classified as %s; a sale needs a REC_ category." % raw_code
                )
                category = None
                cat_source = "rule"
            tax_payment = is_tax_or_social(
                kind, "\n".join([text or "", line_text]), counterparty_name
            )
            asset = bool(
                category is not None and category.code.startswith("EQUIPAMENTO_")
            ) or (direction == "purchase" and is_fixed_asset(line_text))
            tipo = resolve_tipo(
                direction,
                kind,
                category.code if category is not None else None,
                False,
                tax_payment,
                asset,
            )

        if suspect:
            account = ""
        elif settles:
            account = settlement_account(session, kind, direction, counterparty_nif)
        else:
            account = category.snc_account if category is not None and category.snc_account else ""

        decision_confidence = min(extraction_confidence, float(cat_confidence))
        justification = _one_line(cat_justification)
        if extraction_confidence < float(cat_confidence):
            justification = "Extraction confidence %.2f. %s" % (
                extraction_confidence,
                justification,
            )

        status, reason = _review_reason(
            validation, decision_confidence, flags, category, raw_code
        )
        if settles and kind != "payout_report" and not referenced:
            status = "review"
            reason = (reason + "; referenced invoice number missing").strip("; ")

        base = piece["base"]
        vat_amount = piece["vat"]
        total = piece["total"]
        stored_lines = piece_lines
        if credit and not settles:
            base = _negate(base)
            vat_amount = _negate(vat_amount)
            total = _negate(total)
            stored_lines = []
            for line in piece_lines:
                copied = dict(line)
                copied["base"] = _negate(line["base"])
                copied["vat"] = _negate(line["vat"])
                copied["total"] = _negate(line["total"])
                stored_lines.append(copied)
            if "credit note stored negative" not in reason:
                note = "credit note stored negative so it is not a second expense"
                reason = (reason + "; " + note).strip("; ")

        notes_parts = []  # type: List[str]
        if reason:
            notes_parts.append("reasons: " + reason.replace(" | ", " / "))
        notes_parts.append("kind: " + kind)
        notes_parts.append("direction: " + direction)
        notes_parts.append("classification: " + cat_source)
        notes_parts.append("attachment: " + filename)
        issuer_label = party_label(extracted.get("issuer_name"), norm_nif(issuer_nif) or None)
        if issuer_label:
            notes_parts.append("issuer: " + issuer_label)
        if payment_iban:
            notes_parts.append("iban: " + payment_iban)
        if validation.get("foreign_note"):
            notes_parts.append(validation["foreign_note"])
        if flags:
            notes_parts.append("risk_flags: " + "; ".join(flags))
        if suspect:
            notes_parts.append("accounting_entry: no")
            notes_parts.append("excluded_from_linking: yes")
        if settles:
            notes_parts.append("not_revenue: settles an invoice already issued")
            if account:
                notes_parts.append("snc_account: " + account)
        if referenced:
            notes_parts.append("referenced_invoice: " + referenced)
        if kind == "payslip" and validation.get("deductions") is not None:
            notes_parts.append("deductions: " + str(validation.get("deductions")))

        fields = {
            "source_id": sid,
            "period": period_month,
            "document_date": doc_date,
            "payment_date": None,
            "tipo": tipo,
            "category_id": category.id if category is not None else None,
            "entity": counterparty_name or "unknown",
            "nif": _stored_nif(counterparty_nif),
            "document_number": extracted.get("document_number") or filename,
            "base": base,
            "vat_rate": primary_rate(stored_lines) if stored_lines else ZERO,
            "vat": vat_amount,
            "total": total,
            "deductible_vat": deductible_vat(tipo, direction, vat_amount),
            "payment_method": None,
            "status": status,
            "notes": " | ".join(notes_parts),
            "file_path": _rel_path(saved),
            "original_filename": filename,
            "direction": direction,
            "referenced_invoice": referenced or None,
            "lines_json": _lines_json(stored_lines) if stored_lines else None,
            "confidence": decision_confidence,
            "justification": justification or "LLM extraction",
        }
        if not fields["original_filename"]:
            fields["original_filename"] = "attachment.pdf"
        document, skipped = _apply_document(session, current, fields)
        if suspect and document.id is not None:
            links = session.exec(select(Link).where(Link.document_id == document.id)).all()
            for link in links:
                session.delete(link)
            if links:
                session.commit()
        if skipped:
            results.append(_from_existing(document, path.name, filename))
            continue
        new_code = category.code if category is not None else ""
        changes = _changes(
            document.id,
            old,
            new_code,
            tipo,
            party_label(fields["entity"], fields["nif"]),
        )
        if kind == "payout_report":
            payout_document_id = document.id
        results.append(
            _item(
                path.name,
                filename,
                status,
                reason,
                document_id=document.id,
                source_id=sid,
                reprocessed=current is not None,
                extra={
                    "created": current is None,
                    "changes": changes,
                    "validation": validation,
                    "document_kind": kind,
                    "direction": direction,
                    "classification": {
                        "source": cat_source,
                        "category": new_code or None,
                        "raw_category": raw_code,
                        "conta_snc": account or None,
                        "confidence": decision_confidence,
                        "justification": justification,
                    },
                },
            )
        )

    if kind == "payout_report" and lines:
        matched = _missing_sales(
            session,
            path.name,
            source_id,
            period_month,
            lines,
            payout_document_id,
        )
        if matched and payout_document_id is not None:
            payout = session.get(Document, payout_document_id)
            if payout is not None:
                payout.referenced_invoice = ", ".join(matched)
                note = payout.notes or ""
                extra = "referenced_invoice: " + payout.referenced_invoice
                if extra not in note:
                    payout.notes = (note + " | " + extra).strip(" |")
                session.add(payout)
                session.commit()

    return results


def process_eml(session, message, eml_path, categories, reprocess=False):
    # type: (Session, MailMessage, Path, list, bool) -> List[Dict[str, Any]]
    """Process every PDF in one message. One failure does not stop the others."""
    ensure_special_categories(session)
    path = Path(eml_path)
    pdfs = _pdf_attachments(message)
    if not pdfs:
        item = process_email_without_pdf(session, message, path, reprocess=reprocess)
        logger.info("%s %s %s", item["status"], path.name, item.get("reason") or "")
        ensure_owner_tasks(session)
        return [item]

    results = []
    for attachment in pdfs:
        filename = attachment.filename or "attachment.pdf"
        try:
            batch = process_attachment(
                session,
                message,
                attachment,
                path,
                categories,
                reprocess=reprocess,
            )
        except Exception as exc:
            session.rollback()
            reason = "%s: %s" % (type(exc).__name__, exc)
            logger.exception("failed %s %s", path.name, filename)
            batch = [_item(path.name, filename, "error", reason)]
        for item in batch:
            logger.info(
                "%s %s %s %s",
                item["status"],
                path.name,
                filename,
                item.get("reason") or "",
            )
            results.append(item)
    ensure_owner_tasks(session)
    return results


def process_one(eml_path):
    # type: (Path) -> List[Dict[str, Any]]
    init_db()
    path = Path(eml_path)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        raise SystemExit("file not found: %s" % path)

    mailbox = LocalEmlMailbox(path.parent)
    message = mailbox.read(path)
    with Session(engine, expire_on_commit=False) as session:
        categories = list_categories(session)
        if not categories:
            raise SystemExit(
                "Category table is empty. Run python -m app.load_history first."
            )
        return process_eml(session, message, path, categories)


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m app.process_one data/inbox/<file>.eml")
    result = process_one(sys.argv[1])
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()

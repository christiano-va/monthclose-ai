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
    guess_tipo,
    list_categories,
    pdf_text,
    primary_vat,
)
from app.mailbox import Attachment, LocalEmlMailbox, MailMessage
from app.models import Document, Link, OpenItem, Period
from app.money import ZERO
from app.statements import import_statement_text
from app.validate import (
    classify_nif,
    find_ibans,
    normalize_document_kind,
    normalize_iban,
    parse_iso_date,
    parse_money_field,
    resolve_risk_flags,
    validate_extraction,
)

logger = logging.getLogger("app.process")

# Human review when the AI confidence is below this. History reuse is 0.95.
LOW_CONFIDENCE_THRESHOLD = 0.8

_HOURS_RE = re.compile(r"(\d{1,4})\s+horas", re.IGNORECASE)


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
        if hours:
            description = (
                "Accountant: issue invoice to %s for %s approved hours"
                % (counterparty, hours)
            )
        else:
            description = "Accountant: issue invoice to %s" % counterparty
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


def process_attachment(
    session, message, attachment, eml_path, categories, reprocess=False
):
    # type: (Session, MailMessage, Attachment, Path, list, bool) -> Dict[str, Any]
    """Extract, classify, validate, and store one PDF. Raises on hard failure."""
    path = Path(eml_path)
    filename = attachment.filename or "attachment.pdf"
    source_id = attachment_source_id(message, attachment, path)
    existing = _find_stored(session, source_id, _legacy_source_id(path, attachment))
    if existing is not None and not reprocess:
        return _from_existing(existing, path.name, filename)

    text = pdf_text(attachment.data)
    extracted = extract_pdf_document(
        attachment.data, message, categories, text=text
    )
    kind = normalize_document_kind(
        extracted.get("document_kind") or extracted.get("document_type")
    )
    extracted["document_kind"] = kind

    if _is_bank_statement(kind, filename, text):
        return _import_bank_pdf(
            session, message, attachment, path, source_id, existing, text
        )

    category, cat_confidence, cat_justification, cat_source, raw_code = classify_category(
        session, extracted, categories, text, message
    )
    if category is not None:
        extracted["suggested_category"] = category.code
        extracted["conta_snc"] = category.snc_account
    else:
        extracted["suggested_category"] = raw_code or None
        extracted["conta_snc"] = None

    extraction_confidence = float(extracted.get("confidence") or 0)
    decision_confidence = min(extraction_confidence, float(cat_confidence))
    justification = _one_line(cat_justification)
    if extraction_confidence < float(cat_confidence):
        justification = "Extraction confidence %.2f. %s" % (
            extraction_confidence,
            justification,
        )
    extracted["confidence"] = decision_confidence
    extracted["justification"] = justification

    validation = validate_extraction(extracted)
    try:
        base = parse_money_field(validation.get("base"))
        total = parse_money_field(validation.get("total"))
        vat_amount = parse_money_field(validation.get("vat"))
        if kind in ("invoice", "receipt"):
            vat_rate, _parsed_vat = primary_vat(extracted)
        else:
            vat_rate = ZERO
    except (InvalidOperation, ValueError) as exc:
        validation["ok"] = False
        validation["status"] = "review"
        validation["errors"].append("could not parse money amounts: %s" % exc)
        base = ZERO
        total = ZERO
        vat_rate = ZERO
        vat_amount = ZERO

    try:
        doc_date = parse_iso_date(validation.get("document_date") or extracted.get("date"))
    except (TypeError, ValueError):
        doc_date = None
    try:
        due_date = parse_iso_date(validation.get("due_date") or extracted.get("due_date"))
    except (TypeError, ValueError):
        due_date = None

    payment_iban = _payment_iban(extracted, text)
    issuer_known, previous_ibans = _issuer_history(
        session, extracted.get("nif"), source_id
    )
    flag_text = "\n".join(
        [message.subject or "", message.body or "", text or ""]
    )
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
    status, reason = _review_reason(
        validation, decision_confidence, flags, category, raw_code
    )

    if doc_date is None and existing is not None and existing.document_date is not None:
        doc_date = existing.document_date
    if doc_date is None:
        if "cannot store document without a date" not in reason:
            extra = "validation failure: cannot store document without a date"
            reason = (reason + "; " + extra).strip("; ")
            if status == "validated":
                status = "review"
        return _item(
            path.name,
            filename,
            status,
            reason,
            document_id=existing.id if existing is not None else None,
            source_id=source_id,
            reprocessed=False,
            extra={
                "extracted": extracted,
                "validation": validation,
                "document_kind": kind,
            },
        )

    period_month = doc_date.strftime("%Y-%m")
    _get_or_create_period(session, period_month)
    notes_parts = []  # type: List[str]
    if reason:
        notes_parts.append("reasons: " + reason.replace(" | ", " / "))
    notes_parts.append("kind: " + kind)
    notes_parts.append("classification: " + cat_source)
    notes_parts.append("attachment: " + filename)
    if payment_iban:
        notes_parts.append("iban: " + payment_iban)
    if validation.get("foreign_note"):
        notes_parts.append(validation["foreign_note"])
    if flags:
        notes_parts.append("risk_flags: " + "; ".join(flags))
    if kind == "payslip" and validation.get("deductions") is not None:
        notes_parts.append("deductions: " + str(validation.get("deductions")))

    fields = {
        "source_id": source_id,
        "period": period_month,
        "document_date": doc_date,
        "payment_date": None,
        "tipo": guess_tipo(extracted.get("document_type"), kind),
        "category_id": category.id if category is not None else None,
        "entity": extracted.get("issuer_name") or "unknown",
        "nif": _stored_nif(extracted.get("nif")),
        "document_number": extracted.get("document_number") or filename,
        "base": base,
        "vat_rate": vat_rate,
        "vat": vat_amount,
        "total": total,
        "deductible_vat": vat_amount if kind in ("invoice", "receipt") else ZERO,
        "payment_method": None,
        "status": status,
        "notes": " | ".join(notes_parts),
        "file_path": _rel_path(path),
        "confidence": decision_confidence,
        "justification": justification or "LLM extraction",
    }
    document, skipped = _apply_document(session, existing if reprocess else None, fields)
    if skipped:
        return _from_existing(document, path.name, filename)
    return _item(
        path.name,
        filename,
        status,
        reason,
        document_id=document.id,
        source_id=source_id,
        reprocessed=existing is not None and reprocess,
        extra={
            "extracted": extracted,
            "validation": validation,
            "document_kind": kind,
            "classification": {
                "source": cat_source,
                "category": category.code if category is not None else None,
                "raw_category": raw_code,
                "conta_snc": category.snc_account if category is not None else None,
                "confidence": decision_confidence,
                "justification": justification,
            },
        },
    )


def process_eml(session, message, eml_path, categories, reprocess=False):
    # type: (Session, MailMessage, Path, list, bool) -> List[Dict[str, Any]]
    """Process every PDF in one message. One failure does not stop the others."""
    path = Path(eml_path)
    pdfs = _pdf_attachments(message)
    if not pdfs:
        item = process_email_without_pdf(session, message, path, reprocess=reprocess)
        logger.info(
            "%s %s %s",
            item["status"],
            path.name,
            item.get("reason") or "",
        )
        return [item]

    results = []
    for attachment in pdfs:
        filename = attachment.filename or "attachment.pdf"
        try:
            item = process_attachment(
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
            item = _item(path.name, filename, "error", reason)
        logger.info(
            "%s %s %s %s",
            item["status"],
            path.name,
            filename,
            item.get("reason") or "",
        )
        results.append(item)
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

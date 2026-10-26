"""Process a single .eml: extract PDF, classify, validate, store Document."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from sqlmodel import Session, select

from app.db import ROOT, engine, init_db
from app.extract import (
    classify_category,
    extract_pdf_document,
    guess_tipo,
    list_categories,
    lookup_issuer_category,
    pdf_text,
    primary_vat,
)
from app.mailbox import LocalEmlMailbox
from app.models import Document, Period
from app.validate import parse_iso_date, parse_money_field, validate_extraction


def _get_or_create_period(session, month):
    # type: (Session, str) -> Period
    period = session.exec(select(Period).where(Period.month == month)).first()
    if period is None:
        period = Period(month=month, status="open")
        session.add(period)
        session.commit()
        session.refresh(period)
    return period


def _digits(nif):
    # type: (Optional[str]) -> Optional[str]
    if not nif:
        return None
    digits = "".join(ch for ch in str(nif) if ch.isdigit())
    return digits or None


def process_one(eml_path):
    # type: (Path) -> Dict[str, Any]
    init_db()
    path = Path(eml_path)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        raise SystemExit("file not found: %s" % path)

    mailbox = LocalEmlMailbox(path.parent)
    message = mailbox.read(path)
    pdfs = [
        att
        for att in message.attachments
        if att.content_type == "application/pdf"
        or att.filename.lower().endswith(".pdf")
    ]
    if not pdfs:
        raise SystemExit("no PDF attachment in %s" % path.name)

    attachment = pdfs[0]
    source_id = "inbox:%s:%s" % (path.name, attachment.filename)

    with Session(engine) as session:
        existing = session.exec(
            select(Document).where(Document.source_id == source_id)
        ).first()
        categories = list_categories(session)
        if not categories:
            raise SystemExit(
                "Category table is empty. Run python -m app.load_history first."
            )

        text = pdf_text(attachment.data)
        preview_nif = None
        for token in text.replace("/", " ").split():
            digits = "".join(ch for ch in token if ch.isdigit())
            if len(digits) == 9 and digits != "511588135":
                preview_nif = digits
                break
        known = lookup_issuer_category(session, preview_nif)
        extracted = extract_pdf_document(
            attachment.data, message, categories, known_category=known
        )
        category, cat_confidence, cat_justification, cat_source = classify_category(
            session, extracted
        )
        if cat_source == "history" and category is not None:
            extracted["suggested_category"] = category.code
            extracted["conta_snc"] = category.snc_account
            extracted["confidence"] = cat_confidence
            extracted["justification"] = cat_justification

        validation = validate_extraction(extracted)
        status = validation["status"]
        if not validation["ok"]:
            status = "review"

        document_id = None
        if existing is not None:
            document_id = existing.id
        else:
            doc_date = parse_iso_date(extracted.get("date"))
            if doc_date is None:
                status = "review"
                validation["errors"].append("cannot store document without a date")
            else:
                period_month = doc_date.strftime("%Y-%m")
                _get_or_create_period(session, period_month)
                vat_rate, vat_amount = primary_vat(extracted)
                nif = _digits(extracted.get("nif"))
                risk_flags = extracted.get("risk_flags") or []
                notes_parts = []
                if risk_flags:
                    notes_parts.append("risk_flags: " + "; ".join(risk_flags))
                if validation["errors"]:
                    notes_parts.append(
                        "validation: " + "; ".join(validation["errors"])
                    )
                document = Document(
                    source_id=source_id,
                    period=period_month,
                    document_date=doc_date,
                    payment_date=None,
                    tipo=guess_tipo(extracted.get("document_type")),
                    category_id=category.id if category is not None else None,
                    entity=extracted.get("issuer_name") or "unknown",
                    nif=nif,
                    document_number=extracted.get("document_number") or attachment.filename,
                    base=parse_money_field(extracted.get("base")),
                    vat_rate=vat_rate,
                    vat=vat_amount,
                    total=parse_money_field(extracted.get("total")),
                    deductible_vat=vat_amount,
                    payment_method=None,
                    status=status,
                    notes=" | ".join(notes_parts) if notes_parts else None,
                    file_path=str(path.relative_to(ROOT)),
                    confidence=float(extracted.get("confidence") or 0),
                    justification=extracted.get("justification") or cat_justification,
                )
                session.add(document)
                session.commit()
                session.refresh(document)
                document_id = document.id

        return {
            "email": path.name,
            "attachment": attachment.filename,
            "extracted": extracted,
            "classification": {
                "source": cat_source,
                "category": category.code if category is not None else None,
                "conta_snc": category.snc_account if category is not None else None,
                "confidence": cat_confidence,
                "justification": cat_justification,
            },
            "validation": validation,
            "document_id": document_id,
            "status": status,
            "already_existed": existing is not None,
        }


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m app.process_one data/inbox/<file>.eml")
    result = process_one(sys.argv[1])
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()

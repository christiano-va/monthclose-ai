"""Gmail drafts for owner tasks. Drafts are never sent."""
import logging
from typing import Dict

from sqlmodel import Session, select

from app.gmail_api import create_draft
from app.google_auth import GoogleAuthError, gmail_service
from app.models import OpenItem
from app.money import to_money
from app.posting import note_value, set_note
from app.settings import owner_email

logger = logging.getLogger("app.drafts")

DRAFT_NOTE = "gmail_draft_id"
OWNER_KINDS = ("missing_document", "invoice_to_request")


def _address_ok(value):
    # type: (str) -> bool
    text = (value or "").strip()
    if "@" not in text or text.startswith("@") or text.endswith("@"):
        return False
    local, _, domain = text.partition("@")
    return bool(local) and "." in domain and " " not in text


def _subject(item):
    # type: (OpenItem) -> str
    if item.kind == "invoice_to_request":
        return "MonthClose: invoice to request (%s)" % (item.period or "")
    return "MonthClose: missing document (%s)" % (item.period or "")


def _body(item):
    # type: (OpenItem) -> str
    lines = [
        "This draft was prepared by MonthClose AI and has not been sent.",
        "",
        item.description or "",
        "",
        "Period: %s" % (item.period or ""),
        "Amount: %s" % to_money(item.amount),
    ]
    prepared = (item.draft_email or "").strip()
    if prepared:
        lines.extend(["", "Prepared text:", "", prepared])
    return "\n".join(lines)


def create_owner_drafts(session):
    # type: (Session) -> Dict[str, int]
    """One draft per open missing_document or invoice_to_request, addressed to the owner."""
    counts = {
        "created": 0,
        "already_drafted": 0,
        "skipped": 0,
        "failed": 0,
        "owner_email_missing": 0,
    }
    rows = session.exec(
        select(OpenItem).where(OpenItem.status == "open").order_by(OpenItem.id)
    ).all()
    pending = []
    for item in rows:
        if item.kind not in OWNER_KINDS:
            continue
        if note_value(item.notes, DRAFT_NOTE):
            counts["already_drafted"] += 1
            continue
        pending.append(item)
    if not pending:
        return counts

    recipient = owner_email()
    if not _address_ok(recipient):
        counts["skipped"] = len(pending)
        counts["owner_email_missing"] = 1
        return counts

    try:
        service = gmail_service()
    except GoogleAuthError:
        counts["failed"] = len(pending)
        return counts

    for item in pending:
        try:
            draft_id = create_draft(service, recipient, _subject(item), _body(item))
        except Exception as exc:
            logger.error(
                "draft create failed http %s",
                getattr(getattr(exc, "resp", None), "status", ""),
            )
            counts["failed"] += 1
            continue
        if not draft_id:
            counts["failed"] += 1
            continue
        item.notes = set_note(item.notes, DRAFT_NOTE, draft_id)
        session.add(item)
        session.commit()
        counts["created"] += 1
    return counts

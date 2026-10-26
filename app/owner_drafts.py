"""Gmail drafts for owner tasks. Drafts are never sent.

The owner looks for the missing document, asks the supplier or the customer
for a second copy, or issues the sales invoice. The draft is addressed to
OWNER_EMAIL. A task that already has a draft is updated in place.
"""
import logging
import re
from typing import Dict, Optional

from sqlmodel import Session, select

from app.gmail_api import create_draft, update_draft
from app.google_auth import GoogleAuthError, gmail_service
from app.models import BankLine, OpenItem
from app.money import money_or_unknown, to_money
from app.posting import note_value, set_note
from app.settings import owner_email

logger = logging.getLogger("app.drafts")

DRAFT_NOTE = "gmail_draft_id"
OWNER_KINDS = ("missing_document", "missing_sales_invoice", "invoice_to_request")

_INVOICE_PARTY_RE = re.compile(
    r"sales invoice to (?P<party>.+?)(?: for (?P<hours>\d+) hours)?\.?\s*$",
    re.IGNORECASE,
)
_MISSING_PARTY_RE = re.compile(
    r"missing a document for (?P<party>.+?)\.?\s*$",
    re.IGNORECASE,
)
_ON_DATE_RE = re.compile(r"\bon (\d{4}-\d{2}-\d{2})\b")
_HOURS_RE = re.compile(r"\b(\d+)\s+hours\b", re.IGNORECASE)


def _address_ok(value):
    # type: (str) -> bool
    text = (value or "").strip()
    if "@" not in text or text.startswith("@") or text.endswith("@"):
        return False
    local, _, domain = text.partition("@")
    return bool(local) and "." in domain and " " not in text


def _one_line(value):
    # type: (Optional[str]) -> str
    return " ".join((value or "").split())


def _http_status(exc):
    # type: (Exception) -> int
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    try:
        return int(status)
    except (TypeError, ValueError):
        return 0


def _incoming(line):
    # type: (Optional[BankLine]) -> bool
    if line is None:
        return False
    return to_money(line.amount) > 0


def _load_line(session, item):
    # type: (Session, OpenItem) -> Optional[BankLine]
    raw = note_value(item.notes, "bank_line_id")
    if not raw or not str(raw).isdigit():
        return None
    return session.get(BankLine, int(raw))


def _party(item):
    # type: (OpenItem) -> str
    named = _one_line(note_value(item.notes, "counterparty"))
    if named:
        return named
    guest = _one_line(note_value(item.notes, "guest"))
    if guest and guest.lower() != "unknown":
        return guest
    description = item.description or ""
    found = _INVOICE_PARTY_RE.search(description)
    if found:
        return _one_line(found.group("party"))
    found = _MISSING_PARTY_RE.search(description)
    if found:
        return _one_line(found.group("party"))
    return ""


def _hours(item):
    # type: (OpenItem) -> str
    noted = _one_line(note_value(item.notes, "hours"))
    if noted:
        return noted
    found = _INVOICE_PARTY_RE.search(item.description or "")
    if found and found.group("hours"):
        return found.group("hours")
    found = _HOURS_RE.search(item.description or "")
    if found:
        return found.group(1)
    return ""


def _when(item, line):
    # type: (OpenItem, Optional[BankLine]) -> str
    if line is not None and line.movement_date is not None:
        return line.movement_date.isoformat()
    found = _ON_DATE_RE.search(item.description or "")
    if found:
        return found.group(1)
    if item.period:
        return "not stated (period %s)" % item.period
    return "not stated"


def _bank_label(line):
    # type: (Optional[BankLine]) -> str
    if line is None:
        return "not tied to one bank line"
    return "#%s on %s, %s" % (
        line.id,
        line.movement_date.isoformat() if line.movement_date else "",
        _one_line(line.description),
    )


def _whom(item, line):
    # type: (OpenItem, Optional[BankLine]) -> str
    if item.kind in ("missing_sales_invoice", "invoice_to_request"):
        return "customer"
    if line is not None:
        return "customer" if _incoming(line) else "supplier"
    text = (item.description or "").lower()
    if "incoming" in text:
        return "customer"
    if "outgoing" in text:
        return "supplier"
    return "supplier or customer"


def _intro(kind):
    # type: (str) -> str
    if kind == "missing_sales_invoice":
        return "A sales invoice for a stay was not issued."
    if kind == "invoice_to_request":
        return "The customer approved the work, and the sales invoice still has to be issued."
    return "A bank movement is missing its supporting document."


def _action(kind, whom):
    # type: (str, str) -> str
    if kind == "missing_sales_invoice":
        return (
            "Please issue the invoice. If it was already issued and you cannot "
            "find it, ask the customer for a second copy."
        )
    if kind == "invoice_to_request":
        return "Please issue the invoice to the customer."
    if whom == "customer":
        return (
            "Please look for this document. If you cannot find it, ask the "
            "customer for a second copy."
        )
    if whom == "supplier":
        return (
            "Please look for this document. If you cannot find it, ask the "
            "supplier for a second copy."
        )
    return (
        "Please look for this document. If you cannot find it, ask the "
        "supplier or the customer for a second copy."
    )


def build_owner_body(kind, when, amount, counterparty, bank_label, action, hours="", booking=""):
    # type: (str, str, object, str, str, str, str, str) -> str
    """Plain text for the owner. Nothing in this text is sent by itself."""
    lines = [
        "Hello,",
        "",
        _intro(kind),
        "",
        "Date: %s" % when,
        "Amount: %s" % money_or_unknown(amount),
        "Counterparty: %s" % (counterparty or "not named on the task"),
    ]
    if booking:
        lines.append("Booking: %s" % booking)
    if hours:
        lines.append("Hours: %s" % hours)
    lines.extend(
        [
            "Bank line: %s" % bank_label,
            "",
            action,
            "",
            "You can reply to this email with the document attached, or send it through the /scan page.",
            "",
            "This draft was prepared by MonthClose AI and has not been sent.",
        ]
    )
    return "\n".join(lines)


def invoice_request_body(counterparty, hours, period):
    # type: (str, str, str) -> str
    """Stored text for an invoice the owner still has to issue."""
    return build_owner_body(
        "invoice_to_request",
        "not stated (period %s)" % (period or ""),
        None,
        counterparty,
        "not tied to one bank line",
        _action("invoice_to_request", "customer"),
        hours=hours or "",
    )


def _subject(item, counterparty):
    # type: (OpenItem, str) -> str
    period = item.period or ""
    if item.kind == "missing_document":
        return "MonthClose: missing document (%s)" % period
    who = counterparty or "customer"
    return "MonthClose: sales invoice to issue — %s (%s)" % (who, period)


def body_for_item(session, item):
    # type: (Session, OpenItem) -> str
    line = _load_line(session, item)
    party = _party(item)
    hours = _hours(item) if item.kind == "invoice_to_request" else ""
    booking = ""
    if item.kind == "missing_sales_invoice":
        booking = _one_line(note_value(item.notes, "booking"))
    whom = _whom(item, line)
    return build_owner_body(
        item.kind,
        _when(item, line),
        item.amount,
        party,
        _bank_label(line),
        _action(item.kind, whom),
        hours=hours,
        booking=booking,
    )


def _owner_description(item, counterparty, hours):
    # type: (OpenItem, str, str) -> Optional[str]
    """Replace a task line that still asks the accountant to issue the invoice."""
    if item.kind != "invoice_to_request":
        return None
    text = item.description or ""
    if "accountant" not in text.lower() and text.startswith("Owner: issue the sales invoice"):
        return None
    if not counterparty and "accountant" not in text.lower():
        return None
    if hours and counterparty:
        return "Owner: issue the sales invoice to %s for %s hours." % (counterparty, hours)
    if counterparty:
        return "Owner: issue the sales invoice to %s." % counterparty
    return None


def _save_draft(service, draft_id, recipient, subject, body):
    # type: (object, str, str, str, str) -> str
    """Update the stored draft. A missing draft is created. Nothing is sent."""
    if draft_id:
        try:
            updated = update_draft(service, draft_id, recipient, subject, body)
            if updated:
                return updated
        except Exception as exc:
            status = _http_status(exc)
            logger.error("draft update failed http %s", status)
            if status != 404:
                return ""
    try:
        return create_draft(service, recipient, subject, body)
    except Exception as exc:
        logger.error(
            "draft create failed http %s",
            _http_status(exc),
        )
        return ""


def create_owner_drafts(session):
    # type: (Session) -> Dict[str, int]
    """Draft every open owner task. An existing draft is updated and never sent."""
    counts = {
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "owner_email_missing": 0,
    }
    rows = session.exec(
        select(OpenItem).where(OpenItem.status == "open").order_by(OpenItem.id)
    ).all()
    pending = []
    for item in rows:
        if item.kind in OWNER_KINDS:
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
        had_draft = bool(note_value(item.notes, DRAFT_NOTE))
        party = _party(item)
        hours = _hours(item)
        body = body_for_item(session, item)
        subject = _subject(item, party)
        draft_id = _save_draft(
            service,
            note_value(item.notes, DRAFT_NOTE) or "",
            recipient,
            subject,
            body,
        )
        if not draft_id:
            counts["failed"] += 1
            continue
        description = _owner_description(item, party, hours)
        if description:
            item.description = description
        if party:
            item.notes = set_note(item.notes, "counterparty", party)
        if hours and item.kind == "invoice_to_request":
            item.notes = set_note(item.notes, "hours", hours)
        item.draft_email = body
        item.notes = set_note(item.notes, DRAFT_NOTE, draft_id)
        session.add(item)
        session.commit()
        if had_draft:
            counts["updated"] += 1
        else:
            counts["created"] += 1
    return counts

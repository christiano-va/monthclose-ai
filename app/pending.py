"""Match a late document or an explanation to an open pending item.

The sender is never a signal. A match uses the amount, the counterparty
name or tax id, the date and the document type. Closed periods stay closed:
a resolution of one of their items is recorded, not used to reopen the month.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlmodel import Session, select

from app.complete import links_support_line
from app.linker import bank_in, cash_amount, name_tokens
from app.llm import LLMError, llm_json
from app.models import (
    BankLine,
    CreditApplication,
    Document,
    Link,
    OpenItem,
    Period,
    PostCloseResolution,
)
from app.money import ZERO, money_equal, to_money
from app.posting import note_value, set_note, task_assignee
from app.validate import classify_nif

AUTO_CONFIDENCE = 0.90
MEDIUM_CONFIDENCE = 0.50
DATE_NEAR_DAYS = 15

PENDING_KINDS = ("missing_document", "missing_sales_invoice")
RESOLVED_STATUS = "resolved_by_late_document"

_NOISE = {
    "compra",
    "cred",
    "sepa",
    "imed",
    "mbway",
    "cambio",
    "serv",
    "estado",
    "trf",
    "paga",
    "pag",
    "lisboa",
    "porto",
}

_AMOUNT_RE = re.compile(
    r"(?:(?:EUR|€)\s*)(\d{1,3}(?:\.\d{3})*,\d{2}|\d+\.\d{2})"
    r"|(\d{1,3}(?:\.\d{3})*,\d{2}|\d+\.\d{2})(?:\s*(?:EUR|€|euros?))?"
    r"|\b(\d{1,6})\s*(?:EUR|€|euros?)\b",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"\b(\d{4})-(\d{2})-(\d{2})\b|\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b"
)

LATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["candidate_key", "confidence", "justification"],
    "properties": {
        "candidate_key": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "justification": {"type": "string"},
    },
}

EXPLANATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "candidate_key",
        "confidence",
        "justification",
        "classification",
        "account",
        "category_code",
        "counterparty",
        "nif",
    ],
    "properties": {
        "candidate_key": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "justification": {"type": "string"},
        "classification": {"type": "string"},
        "account": {"type": "string"},
        "category_code": {"type": "string"},
        "counterparty": {"type": "string"},
        "nif": {"type": "string"},
    },
}


def _one_line(text):
    # type: (Optional[str]) -> str
    return " ".join(str(text or "").split())


def _compact(value):
    # type: (Optional[str]) -> str
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


def _kind(document):
    # type: (Document) -> str
    return note_value(document.notes, "kind") or ""


def _doc_amount(document):
    # type: (Document) -> Optional[Decimal]
    amount = cash_amount(document)
    if amount is None and document.total is not None:
        amount = to_money(abs(document.total))
    if amount is None or amount <= ZERO:
        return None
    return to_money(amount)


def _line_amount(line):
    # type: (BankLine) -> Decimal
    return to_money(abs(line.amount))


def _period_closed(session, month):
    # type: (Session, Optional[str]) -> bool
    if not month:
        return False
    period = session.exec(select(Period).where(Period.month == month)).first()
    return period is not None and period.status == "closed"


def _party_tokens(*blobs):
    # type: (str) -> List[str]
    found = []  # type: List[str]
    for blob in blobs:
        for token in name_tokens(blob or ""):
            if token in _NOISE or token in found:
                continue
            found.append(token)
    return found


def _tokens_hit(tokens, blob):
    # type: (Sequence[str], str) -> List[str]
    text = (blob or "").lower()
    compact = "".join(ch for ch in text if ch.isalnum())
    hits = []  # type: List[str]
    for token in tokens:
        if token in text or token in compact:
            hits.append(token)
    return hits


def _parse_amounts(text):
    # type: (str) -> List[Decimal]
    found = []  # type: List[Decimal]
    for match in _AMOUNT_RE.finditer(text or ""):
        raw = next(group for group in match.groups() if group)
        normalized = raw.replace(".", "").replace(",", ".") if "," in raw else raw
        if "," in raw:
            normalized = raw.replace(".", "").replace(",", ".")
        try:
            amount = to_money(normalized)
        except Exception:
            continue
        if amount <= ZERO or amount in found:
            continue
        found.append(amount)
    return found


def _parse_dates(text, anchor):
    # type: (str, Optional[datetime]) -> List[date]
    year_hint = anchor.year if anchor is not None else date.today().year
    found = []  # type: List[date]
    for match in _DATE_RE.finditer(text or ""):
        parsed = None  # type: Optional[date]
        if match.group(1):
            try:
                parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                parsed = None
        else:
            day = int(match.group(4))
            month = int(match.group(5))
            year_raw = match.group(6)
            year = year_hint
            if year_raw:
                year = int(year_raw)
                if year < 100:
                    year += 2000
            try:
                parsed = date(year, month, day)
            except ValueError:
                parsed = None
            if parsed is not None and anchor is not None and parsed > anchor.date():
                try:
                    parsed = date(year - 1, month, day)
                except ValueError:
                    parsed = None
        if parsed is not None and parsed not in found:
            found.append(parsed)
    return found


def _near(left, right):
    # type: (Optional[date], Optional[date]) -> bool
    if left is None or right is None:
        return False
    return abs((left - right).days) <= DATE_NEAR_DAYS


def _document_dates(document, extra_text):
    # type: (Document, str) -> List[date]
    found = []  # type: List[date]
    if document.document_date is not None:
        found.append(document.document_date)
    if document.payment_date is not None and document.payment_date not in found:
        found.append(document.payment_date)
    for parsed in _parse_dates(extra_text or "", None):
        if parsed not in found:
            found.append(parsed)
    return found


def _candidate_key(candidate):
    # type: (Dict[str, Any]) -> str
    item = candidate.get("item")
    line = candidate.get("line")
    if item is not None and item.id is not None:
        return "item:%s" % item.id
    if line is not None and line.id is not None:
        return "line:%s" % line.id
    return ""


def _target_amounts(candidate):
    # type: (Dict[str, Any]) -> List[Decimal]
    amounts = []  # type: List[Decimal]
    item = candidate.get("item")
    line = candidate.get("line")
    if item is not None and item.amount is not None:
        amounts.append(to_money(abs(item.amount)))
    if line is not None:
        bank = _line_amount(line)
        if bank not in amounts:
            amounts.append(bank)
    return amounts


def _amount_exact(amount, candidate):
    # type: (Optional[Decimal], Dict[str, Any]) -> bool
    if amount is None:
        return False
    for target in _target_amounts(candidate):
        if money_equal(amount, target):
            return True
    return False


def _party_hits(document, candidate, context):
    # type: (Document, Dict[str, Any], str) -> List[str]
    item = candidate.get("item")
    line = candidate.get("line")
    doc_tokens = _party_tokens(document.entity, document.notes or "")
    nif = "".join(ch for ch in (document.nif or "") if ch.isdigit())
    blobs = [context or ""]
    if line is not None:
        blobs.append(line.description or "")
    if item is not None:
        blobs.append(item.description or "")
        blobs.append(item.notes or "")
    blob = "\n".join(blobs)
    hits = _tokens_hit(doc_tokens, blob)
    if nif and len(nif) == 9 and nif in "".join(ch for ch in blob if ch.isdigit()):
        hits.append(nif)
    number = _compact(document.document_number or "")
    compact_blob = _compact(blob)
    if len(number) >= 6 and number in compact_blob and number not in hits:
        hits.append(number)
    for token in re.findall(r"\b[A-Z]{2,}\d[A-Z0-9]{2,}\b", (document.notes or "").upper()):
        if token in compact_blob or token in (blob or "").upper():
            if token not in hits:
                hits.append(token)
    return hits


def _type_ok(document, candidate):
    # type: (Document, Dict[str, Any]) -> bool
    if document.total is not None and document.total < ZERO:
        return False
    if _kind(document) in ("payout_report", "bank_statement"):
        return False
    item = candidate.get("item")
    line = candidate.get("line")
    is_sale = document.direction == "sale" or document.tipo == "receita"
    is_purchase = document.direction == "purchase" or document.tipo in (
        "gasto",
        "investimento",
    )
    if item is not None and item.kind == "missing_sales_invoice":
        return is_sale
    incoming = None  # type: Optional[bool]
    if line is not None:
        incoming = bank_in(line)
    if incoming is True:
        return is_sale
    if incoming is False:
        return is_purchase
    return True


def _date_ok(document, candidate, context):
    # type: (Document, Dict[str, Any], str) -> bool
    line = candidate.get("line")
    if line is None or line.movement_date is None:
        return False
    for item in _document_dates(document, context):
        if _near(item, line.movement_date):
            return True
    return False


def _signals(document, candidate, context):
    # type: (Document, Dict[str, Any], str) -> Dict[str, Any]
    amount = _doc_amount(document)
    return {
        "amount": _amount_exact(amount, candidate),
        "party": bool(_party_hits(document, candidate, context)),
        "date": _date_ok(document, candidate, context),
        "type_ok": _type_ok(document, candidate),
        "amount_value": amount,
    }


def _confidence_from(signals):
    # type: (Dict[str, Any]) -> Tuple[float, str]
    if not signals["amount"]:
        if signals["party"] and signals["date"] and signals["type_ok"]:
            return 0.4, "Counterparty, date and type agree but the amount differs."
        return 0.0, "No amount match."
    score = 0.55
    reasons = ["amount matches"]
    if signals["party"]:
        score += 0.20
        reasons.append("counterparty matches the line or the task")
    if signals["date"]:
        score += 0.15
        reasons.append("date is near the bank line")
    if signals["type_ok"]:
        score += 0.10
        reasons.append("document type is compatible")
    if score > 0.97:
        score = 0.97
    return score, _one_line("; ".join(reasons) + ".")


def _load_candidates(session):
    # type: (Session) -> List[Dict[str, Any]]
    """Open or owner-accepted pendings, plus unsupported lines of any period."""
    covered = set()  # type: set
    found = []  # type: List[Dict[str, Any]]
    items = session.exec(select(OpenItem).order_by(OpenItem.id)).all()
    for item in items:
        if item.kind not in PENDING_KINDS:
            continue
        if item.status not in ("open", "accepted_by_owner"):
            continue
        line = None  # type: Optional[BankLine]
        raw = note_value(item.notes, "bank_line_id")
        if raw and str(raw).isdigit():
            line = session.get(BankLine, int(raw))
            if line is not None and line.id is not None:
                covered.add(line.id)
        found.append({"item": item, "line": line})
    lines = session.exec(select(BankLine).order_by(BankLine.id)).all()
    for line in lines:
        if line.status != "unsupported" or line.id in covered:
            continue
        found.append({"item": None, "line": line})
    return found


def _allocated(session, document):
    # type: (Session, Document) -> Decimal
    total = ZERO
    if document.id is None:
        return total
    links = session.exec(select(Link).where(Link.document_id == document.id)).all()
    for link in links:
        amount = to_money(link.allocated_amount)
        if amount > ZERO:
            total = to_money(total + amount)
    return total


def document_fully_linked(session, document):
    # type: (Session, Document) -> bool
    cap = _doc_amount(document)
    if cap is None:
        return False
    used = _allocated(session, document)
    return used > ZERO and used + Decimal("0.01") >= cap


def _skip_document(document):
    # type: (Document) -> bool
    if document.status in ("duplicate", "error", "no_document", "bank_statement"):
        return True
    if "excluded_from_linking: yes" in (document.notes or ""):
        return True
    if _kind(document) in ("payout_report", "bank_statement"):
        return True
    if document.total is not None and document.total < ZERO:
        return True
    return False


def _public_candidate(candidate):
    # type: (Dict[str, Any]) -> Dict[str, Any]
    item = candidate.get("item")
    line = candidate.get("line")
    row = {
        "candidate_key": _candidate_key(candidate),
        "amounts": [str(amount) for amount in _target_amounts(candidate)],
    }
    if item is not None:
        row["open_item_id"] = item.id
        row["kind"] = item.kind
        row["status"] = item.status
        row["period"] = item.period
        row["description"] = item.description
        row["item_amount"] = str(to_money(abs(item.amount)))
    if line is not None:
        row["bank_line_id"] = line.id
        row["line_date"] = line.movement_date.isoformat()
        row["line_amount"] = str(_line_amount(line))
        row["line_description"] = line.description
        row["line_period"] = line.period
        row["direction"] = "incoming" if bank_in(line) else "outgoing"
    return row


def _ask_llm(document, context, ranked):
    # type: (Document, str, Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]
    payload = []
    for row in ranked[:8]:
        payload.append(_public_candidate(row["candidate"]))
    prompt = "\n".join(
        [
            "A document arrived after a bookkeeping month was processed.",
            "Choose which pending item it supports, or none.",
            "Match only on content: amount, counterparty name or NIF, date, and document type (purchase or sale).",
            "Who sent the message is irrelevant. Do not use the sender.",
            "candidate_key must be copied from the list, or an empty string when nothing matches.",
            "confidence is 0-1. justification is one line.",
            "A sales invoice can match a missing_sales_invoice whose amount is the gross, even when the bank credit is the gross minus a platform fee.",
            "A purchase invoice matches an outgoing bank line. A sales invoice matches an incoming line.",
            "",
            "Document %s dated %s entity %s nif %s total %s direction %s type %s."
            % (
                document.document_number,
                document.document_date.isoformat() if document.document_date else "",
                document.entity,
                document.nif or "",
                document.total,
                document.direction or "",
                _kind(document) or document.tipo,
            ),
            "Message text (may be empty):",
            context or "(none)",
            "",
            "Candidates:",
            json.dumps(payload, ensure_ascii=False),
        ]
    )
    try:
        return llm_json(prompt, LATE_SCHEMA)
    except (LLMError, Exception):
        return None


def _score_document(document, candidates, context):
    # type: (Document, Sequence[Dict[str, Any]], str) -> List[Dict[str, Any]]
    ranked = []  # type: List[Dict[str, Any]]
    for candidate in candidates:
        signals = _signals(document, candidate, context)
        confidence, reason = _confidence_from(signals)
        if confidence < 0.4 and not signals["amount"]:
            continue
        ranked.append(
            {
                "candidate": candidate,
                "signals": signals,
                "confidence": confidence,
                "reason": reason,
            }
        )
    ranked.sort(
        key=lambda row: (
            -row["confidence"],
            0 if row["signals"]["amount"] else 1,
            _candidate_key(row["candidate"]),
        )
    )
    return ranked


def _ambiguous(ranked):
    # type: (Sequence[Dict[str, Any]]) -> bool
    if len(ranked) < 2:
        return False
    first = ranked[0]
    second = ranked[1]
    if not first["signals"]["amount"] or not second["signals"]["amount"]:
        return False
    return abs(first["confidence"] - second["confidence"]) < 0.05


def _all_clear(signals):
    # type: (Dict[str, Any]) -> bool
    return bool(
        signals["amount"] and signals["party"] and signals["date"] and signals["type_ok"]
    )


def _choose(document, candidates, context):
    # type: (Document, Sequence[Dict[str, Any]], str) -> Optional[Dict[str, Any]]
    ranked = _score_document(document, candidates, context)
    if not ranked:
        return None
    ambiguous = _ambiguous(ranked)
    chosen = ranked[0]
    confidence = float(chosen["confidence"])
    justification = chosen["reason"]
    has_context = bool(_one_line(context))
    if has_context:
        reply = _ask_llm(document, context, ranked)
        if reply is not None:
            key = (reply.get("candidate_key") or "").strip()
            picked = None
            for row in ranked:
                if _candidate_key(row["candidate"]) == key:
                    picked = row
                    break
            model_confidence = float(reply.get("confidence") or 0)
            model_reason = _one_line(reply.get("justification") or "")
            if picked is None:
                if _all_clear(chosen["signals"]) and not ambiguous:
                    confidence = chosen["confidence"]
                    justification = _one_line(
                        "%s The message did not name a different item." % chosen["reason"]
                    )
                else:
                    return None
            else:
                chosen = picked
                confidence = model_confidence
                justification = model_reason or picked["reason"]
                if not picked["signals"]["amount"]:
                    confidence = min(confidence, 0.89)
                elif _all_clear(picked["signals"]):
                    confidence = max(confidence, float(picked["confidence"]))
    if ambiguous and not has_context:
        confidence = min(confidence, 0.74)
        justification = _one_line(
            "%s More than one item has the same score." % justification
        )
    if confidence < MEDIUM_CONFIDENCE:
        return None
    return {
        "candidate": chosen["candidate"],
        "signals": chosen["signals"],
        "confidence": confidence,
        "justification": justification or "Pending item match.",
        "ambiguous": ambiguous,
    }


def _links_of(session, line):
    # type: (Session, BankLine) -> List[Link]
    if line.id is None:
        return []
    return session.exec(select(Link).where(Link.bank_line_id == line.id)).all()


def _remember_history(item):
    # type: (OpenItem) -> None
    """Keep the owner acceptance on the item when a later document resolves it."""
    if item.status == "accepted_by_owner" or item.approved_by or item.approval_reason:
        stamp = ""
        if item.approved_at is not None:
            stamp = item.approved_at.isoformat()
        item.notes = set_note(item.notes, "history_status", "accepted_by_owner")
        item.notes = set_note(item.notes, "history_approved_by", item.approved_by or "")
        item.notes = set_note(item.notes, "history_approved_at", stamp)
        item.notes = set_note(item.notes, "history_reason", item.approval_reason or "")


def _history_text(item, document):
    # type: (OpenItem, Document) -> str
    previous = note_value(item.notes, "history_status") or item.status
    who = note_value(item.notes, "history_approved_by") or item.approved_by or ""
    reason = note_value(item.notes, "history_reason") or item.approval_reason or ""
    when = ""
    if item.resolved_at is not None:
        when = item.resolved_at.date().isoformat()
    piece = "%s -> %s" % (previous, RESOLVED_STATUS)
    if who:
        piece += " (accepted by %s" % who
        if reason:
            piece += ": %s" % reason
        piece += ")"
    if when:
        piece += " at %s" % when
    piece += ", document %s" % (document.document_number or document.id)
    return _one_line(piece)


def _mark_resolved(session, item, document, line, confidence, justification, email_source):
    # type: (Session, OpenItem, Document, Optional[BankLine], float, str, Optional[str]) -> Optional[int]
    _remember_history(item)
    now = datetime.utcnow()
    item.status = RESOLVED_STATUS
    item.resolved_at = now
    item.resolved_document_id = document.id
    item.confidence = float(confidence)
    item.justification = _one_line(justification)
    item.notes = set_note(item.notes, "resolved_by", "late_document")
    item.notes = set_note(item.notes, "resolved_document_id", str(document.id))
    session.add(item)
    post_id = None  # type: Optional[int]
    period = item.period
    line_period = line.period if line is not None else None
    if _period_closed(session, period) or _period_closed(session, line_period):
        summary = _one_line(
            "Late resolution of %s by %s. %s"
            % (item.description, document.document_number or document.id, justification)
        )
        row = PostCloseResolution(
            period=period,
            open_item_id=item.id,
            bank_line_id=line.id if line is not None else None,
            document_id=document.id,
            email_source_id=email_source,
            resolved_at=now,
            summary=summary,
        )
        session.add(row)
        session.flush()
        post_id = row.id
    return post_id


def _existing_link(session, document_id, line_id):
    # type: (Session, int, int) -> Optional[Link]
    return session.exec(
        select(Link).where(
            Link.document_id == document_id,
            Link.bank_line_id == line_id,
        )
    ).first()


def _link_amount(session, document, line, amount, method, explanation, confidence):
    # type: (Session, Document, BankLine, Decimal, str, str, float) -> bool
    if document.id is None or line.id is None:
        return False
    amount = to_money(amount)
    if amount <= ZERO:
        return False
    if _existing_link(session, document.id, line.id) is not None:
        return False
    cap = _doc_amount(document)
    if cap is not None and _allocated(session, document) + amount - cap > Decimal("0.01"):
        return False
    current = _links_of(session, line)
    running = ZERO
    for link in current:
        running = to_money(running + to_money(link.allocated_amount))
    if not money_equal(to_money(running + amount), _line_amount(line)):
        return False
    link = Link(
        document_id=document.id,
        bank_line_id=line.id,
        allocated_amount=amount,
        method=method,
        explanation=_one_line(explanation),
        confidence=float(confidence),
    )
    session.add(link)
    session.flush()
    linked = _links_of(session, line)
    if not links_support_line(line, linked):
        session.delete(link)
        session.flush()
        return False
    line.status = "linked"
    session.add(line)
    return True


def _relink_payout(session, document, line, gross, explanation, confidence):
    # type: (Session, Document, BankLine, Decimal, str, float) -> bool
    """The sales invoice carries the gross. The payout report becomes evidence."""
    if document.id is None or line.id is None:
        return False
    links = _links_of(session, line)
    report = None  # type: Optional[Link]
    for link in links:
        if link.method == "payout" and to_money(link.allocated_amount) > ZERO:
            if report is None or to_money(link.allocated_amount) > to_money(report.allocated_amount):
                report = link
    if report is None:
        return False
    if not money_equal(to_money(report.allocated_amount), gross):
        return False
    if _existing_link(session, document.id, line.id) is not None:
        return False
    running = ZERO
    for link in links:
        if link.id == report.id:
            continue
        running = to_money(running + to_money(link.allocated_amount))
    if not money_equal(to_money(running + gross), _line_amount(line)):
        return False
    report.allocated_amount = ZERO
    report.method = "evidence"
    report.explanation = _one_line(
        "Payout report is evidence; %s carries the gross %s."
        % (document.document_number, to_money(gross))
    )
    report.confidence = float(confidence)
    session.add(report)
    link = Link(
        document_id=document.id,
        bank_line_id=line.id,
        allocated_amount=to_money(gross),
        method="late_document",
        explanation=_one_line(explanation),
        confidence=float(confidence),
    )
    session.add(link)
    session.flush()
    if not links_support_line(line, _links_of(session, line)):
        session.delete(link)
        report.allocated_amount = to_money(gross)
        report.method = "payout"
        session.add(report)
        session.flush()
        return False
    line.status = "linked"
    session.add(line)
    return True


def _needs_payout_relink(candidate):
    # type: (Dict[str, Any]) -> bool
    item = candidate.get("item")
    return item is not None and item.kind == "missing_sales_invoice"


def _stamp_document(document, confidence, justification, outcome):
    # type: (Document, float, str, str) -> None
    document.notes = set_note(document.notes, "late_outcome", outcome)
    document.notes = set_note(document.notes, "late_match_confidence", "%.2f" % confidence)
    document.notes = set_note(document.notes, "late_match_justification", _one_line(justification))
    document.confidence = float(confidence)
    document.justification = _one_line(justification)


def _find_task(session, marker):
    # type: (Session, str) -> Optional[OpenItem]
    rows = session.exec(select(OpenItem)).all()
    for item in rows:
        if item.kind not in ("confirm_late_document", "confirm_explanation"):
            continue
        if item.notes and marker in item.notes:
            return item
    return None


def _confirm_task(session, kind, candidate, document, confidence, justification, email_name):
    # type: (Session, str, Dict[str, Any], Document, float, str, str) -> OpenItem
    item = candidate.get("item")
    line = candidate.get("line")
    period = "1970-01"
    if item is not None and item.period:
        period = item.period
    elif line is not None and line.period:
        period = line.period
    if session.exec(select(Period).where(Period.month == period)).first() is None:
        session.add(Period(month=period, status="open"))
        session.flush()
    target = item.description if item is not None else (line.description if line is not None else "a bank line")
    if kind == "confirm_explanation":
        description = (
            "Owner: confirm the explanation matches %s. %s"
            % (target, _one_line(justification))
        )
    else:
        description = (
            "Owner: confirm late document %s (%s) matches %s. %s"
            % (document.document_number, document.total, target, _one_line(justification))
        )
    notes = [
        "confirm_for_document: %s" % document.id,
        "confidence: %.2f" % confidence,
        "reasons: %s" % _one_line(justification),
    ]
    if email_name:
        notes.append("email: %s" % email_name)
    if item is not None and item.id is not None:
        notes.append("proposed_open_item_id: %s" % item.id)
    if line is not None and line.id is not None:
        notes.append("proposed_bank_line_id: %s" % line.id)
    marker = "confirm_for_document: %s" % document.id
    existing = _find_task(session, marker)
    if existing is None:
        existing = OpenItem(
            document_id=document.id,
            period=period,
            kind=kind,
            description=description,
            amount=to_money(abs(item.amount)) if item is not None else _line_amount(line),
            due_date=None,
            status="open",
            notes=" | ".join(notes),
            assignee=task_assignee(kind),
            confidence=float(confidence),
            justification=_one_line(justification),
        )
        session.add(existing)
    else:
        existing.kind = kind
        existing.description = description
        existing.period = period
        existing.status = "open"
        existing.notes = " | ".join(notes)
        existing.confidence = float(confidence)
        existing.justification = _one_line(justification)
        existing.assignee = task_assignee(kind)
        session.add(existing)
    session.flush()
    return existing


def _decision_payload(outcome, document, choice, history, post_id, task_id, email_name):
    # type: (str, Document, Dict[str, Any], str, Optional[int], Optional[int], str) -> Dict[str, Any]
    candidate = choice["candidate"]
    item = candidate.get("item")
    line = candidate.get("line")
    return {
        "outcome": outcome,
        "confidence": round(float(choice["confidence"]), 4),
        "justification": choice.get("justification") or choice.get("reason") or "",
        "exact_amount": bool(choice["signals"]["amount"]),
        "open_item_id": item.id if item is not None else None,
        "item_kind": item.kind if item is not None else None,
        "item_status": item.status if item is not None else None,
        "item": item.description if item is not None else None,
        "bank_line_id": line.id if line is not None else None,
        "document_id": document.id,
        "document_number": document.document_number,
        "email": email_name,
        "history": history,
        "post_close_id": post_id,
        "confirm_task_id": task_id,
        "method": "late_document" if outcome == "linked" else "",
    }


def _exact_line_set(document, candidates, context):
    # type: (Document, Sequence[Dict[str, Any]], str) -> Optional[List[Dict[str, Any]]]
    """Lines whose amounts sum to the document, when no single line does."""
    target = _doc_amount(document)
    if target is None:
        return None
    usable = []  # type: List[Dict[str, Any]]
    for candidate in candidates:
        line = candidate.get("line")
        if line is None:
            continue
        piece = _line_amount(line)
        if piece <= ZERO or piece - target > Decimal("0.01"):
            continue
        if _amount_exact(target, candidate):
            continue
        if not _type_ok(document, candidate):
            continue
        if not _party_hits(document, candidate, context):
            continue
        usable.append(candidate)
    found = []  # type: List[List[Dict[str, Any]]]

    def walk(start, chosen, total):
        # type: (int, List[Dict[str, Any]], Decimal) -> None
        if len(found) > 1:
            return
        if len(chosen) >= 2 and money_equal(total, target):
            found.append(list(chosen))
            return
        if len(chosen) >= 4:
            return
        index = start
        while index < len(usable):
            piece = _line_amount(usable[index]["line"])
            if total + piece - target <= Decimal("0.01"):
                chosen.append(usable[index])
                walk(index + 1, chosen, to_money(total + piece))
                chosen.pop()
            index += 1

    walk(0, [], ZERO)
    if len(found) != 1:
        return None
    return found[0]


def _resolve_line_set(session, document, group, context, email_name, email_source):
    # type: (Session, Document, Sequence[Dict[str, Any]], str, str, Optional[str]) -> Dict[str, Any]
    dates_ok = all(_date_ok(document, candidate, context) for candidate in group)
    confidence = 0.93 if dates_ok else 0.72
    justification = _one_line(
        "Document %s equals the sum of %d bank lines of the same counterparty."
        % (document.document_number, len(group))
    )
    linked_ids = []  # type: List[int]
    for candidate in group:
        line = candidate.get("line")
        if line is not None and line.id is not None:
            linked_ids.append(line.id)
    if confidence < AUTO_CONFIDENCE:
        _stamp_document(document, confidence, justification, "confirm")
        session.add(document)
        task = _confirm_task(
            session,
            "confirm_late_document",
            group[0],
            document,
            confidence,
            justification,
            email_name,
        )
        session.commit()
        return {
            "outcome": "confirm",
            "confidence": confidence,
            "justification": justification,
            "document_id": document.id,
            "document_number": document.document_number,
            "confirm_task_id": task.id,
            "bank_line_ids": linked_ids,
            "email": email_name,
            "method": "late_document",
        }
    for candidate in group:
        line = candidate["line"]
        if not _link_amount(
            session,
            document,
            line,
            _line_amount(line),
            "late_document",
            justification,
            confidence,
        ):
            session.rollback()
            return {
                "outcome": "none",
                "document_id": document.id,
                "confidence": 0,
                "justification": "The line set could not be linked.",
            }
    if confidence >= AUTO_CONFIDENCE:
        histories = []  # type: List[str]
        post_id = None  # type: Optional[int]
        for candidate in group:
            item = candidate.get("item")
            if item is None:
                continue
            post_id = _mark_resolved(
                session,
                item,
                document,
                candidate.get("line"),
                confidence,
                justification,
                email_source,
            )
            histories.append(_history_text(item, document))
        _stamp_document(document, confidence, justification, "linked")
        document.notes = set_note(document.notes, "fully_linked", "yes")
        session.add(document)
        session.commit()
        return {
            "outcome": "linked",
            "confidence": confidence,
            "justification": justification,
            "exact_amount": True,
            "open_item_id": group[0]["item"].id if group[0].get("item") is not None else None,  # noqa: the item row is optional
            "bank_line_ids": linked_ids,
            "document_id": document.id,
            "document_number": document.document_number,
            "email": email_name,
            "history": " | ".join(histories),
            "post_close_id": post_id,
            "method": "late_document",
        }


def resolve_late_document(session, document, context, email_name="", email_source=None):
    # type: (Session, Document, str, str, Optional[str]) -> Dict[str, Any]
    """Link a new document to one pending item, or ask the owner, or leave it."""
    if document.id is None or _skip_document(document) or document_fully_linked(session, document):
        return {"outcome": "none", "document_id": document.id, "confidence": 0}
    if note_value(document.notes, "late_outcome") == "linked":
        return {
            "outcome": "linked",
            "document_id": document.id,
            "confidence": float(note_value(document.notes, "late_match_confidence") or 0),
            "justification": note_value(document.notes, "late_match_justification") or "",
            "already": True,
        }
    candidates = _load_candidates(session)
    choice = _choose(document, candidates, context or "")
    if choice is None:
        group = _exact_line_set(document, candidates, context or "")
        if group:
            return _resolve_line_set(
                session, document, group, context or "", email_name, email_source
            )
        return {
            "outcome": "none",
            "document_id": document.id,
            "confidence": 0,
            "justification": "No pending item matches this document.",
        }
    confidence = float(choice["confidence"])
    exact = bool(choice["signals"]["amount"])
    candidate = choice["candidate"]
    item = candidate.get("item")
    line = candidate.get("line")
    if confidence >= AUTO_CONFIDENCE and exact:
        linked = False
        if _needs_payout_relink(candidate) and line is not None:
            gross = to_money(abs(item.amount)) if item is not None else _doc_amount(document)
            linked = _relink_payout(
                session,
                document,
                line,
                gross or ZERO,
                choice["justification"],
                confidence,
            )
        elif line is not None:
            linked = _link_amount(
                session,
                document,
                line,
                _line_amount(line),
                "late_document",
                choice["justification"],
                confidence,
            )
        if linked:
            post_id = None  # type: Optional[int]
            history = ""
            if item is not None:
                post_id = _mark_resolved(
                    session,
                    item,
                    document,
                    line,
                    confidence,
                    choice["justification"],
                    email_source,
                )
            _stamp_document(document, confidence, choice["justification"], "linked")
            document.notes = set_note(document.notes, "fully_linked", "yes")
            session.add(document)
            session.commit()
            if item is not None:
                session.refresh(item)
                history = _history_text(item, document)
            payload = _decision_payload(
                "linked", document, choice, history, post_id, None, email_name
            )
            payload["history"] = history
            return payload
        if not linked:
            session.rollback()
            confidence = min(confidence, 0.85)
            choice["confidence"] = confidence
            choice["justification"] = _one_line(
                "%s The amounts could not be linked without breaking the bank line."
                % choice["justification"]
            )
    if confidence >= MEDIUM_CONFIDENCE:
        _stamp_document(document, confidence, choice["justification"], "confirm")
        session.add(document)
        task = _confirm_task(
            session,
            "confirm_late_document",
            candidate,
            document,
            confidence,
            choice["justification"],
            email_name,
        )
        session.commit()
        return _decision_payload(
            "confirm", document, choice, "", None, task.id, email_name
        )
    return {
        "outcome": "none",
        "document_id": document.id,
        "confidence": confidence,
        "justification": choice["justification"],
    }


def _text_party(text, candidate):
    # type: (str, Dict[str, Any]) -> List[str]
    item = candidate.get("item")
    line = candidate.get("line")
    blobs = []
    if line is not None:
        blobs.append(line.description or "")
    if item is not None:
        blobs.append(item.description or "")
    tokens = _party_tokens(*blobs)
    return _tokens_hit(tokens, text)


def _text_dates_ok(dates, candidate):
    # type: (Sequence[date], Dict[str, Any]) -> bool
    line = candidate.get("line")
    if line is None:
        return False
    for parsed in dates:
        if _near(parsed, line.movement_date):
            return True
    return False


def _text_type_ok(text, candidate):
    # type: (str, Dict[str, Any]) -> bool
    line = candidate.get("line")
    item = candidate.get("item")
    lowered = (text or "").lower()
    incoming_words = ("transferi", "sinal", "adiantamento", "paguei", "envio", "enviei")
    if item is not None and item.kind == "missing_sales_invoice":
        return "fatura" in lowered or "estadia" in lowered
    if line is None:
        return False
    if bank_in(line):
        return any(word in lowered for word in incoming_words) or "nif" in lowered
    return "fatura" in lowered or "compra" in lowered or "pagamento" in lowered


def _score_text(text, amounts, dates, candidate):
    # type: (str, Sequence[Decimal], Sequence[date], Dict[str, Any]) -> Dict[str, Any]
    exact = False
    matched = None  # type: Optional[Decimal]
    for amount in amounts:
        if _amount_exact(amount, candidate):
            exact = True
            matched = amount
            break
    signals = {
        "amount": exact,
        "party": bool(_text_party(text, candidate)),
        "date": _text_dates_ok(dates, candidate),
        "type_ok": _text_type_ok(text, candidate),
        "amount_value": matched,
    }
    confidence, reason = _confidence_from(signals)
    return {
        "candidate": candidate,
        "signals": signals,
        "confidence": confidence,
        "reason": reason,
    }


def _advance_text(text):
    # type: (str) -> bool
    lowered = (text or "").lower()
    return "sinal" in lowered or "adiantament" in lowered


def _nif_in_text(nif, text):
    # type: (str, str) -> bool
    digits = "".join(ch for ch in (nif or "") if ch.isdigit())
    if len(digits) != 9:
        return False
    compact = "".join(ch for ch in (text or "") if ch.isdigit())
    return digits in compact


def _name_in_text(name, text):
    # type: (str, str) -> bool
    hits = _tokens_hit(_party_tokens(name), text)
    return bool(hits)


def _ask_explanation(text, ranked, categories):
    # type: (str, Sequence[Dict[str, Any]], Sequence[Any]) -> Optional[Dict[str, Any]]
    payload = [_public_candidate(row["candidate"]) for row in ranked[:8]]
    category_lines = []
    for category in categories:
        category_lines.append(
            "%s | %s | %s" % (category.code, category.snc_account or "", category.description or "")
        )
    prompt = "\n".join(
        [
            "This email has no attachment. It may explain a bank line that still has no document.",
            "Match only on the text: amount, counterparty name or NIF, date, and whether the money is incoming or outgoing.",
            "Who sent the email is irrelevant.",
            "candidate_key is copied from the list, or empty when nothing matches.",
            "When the text says the transfer is a sinal or an adiantamento before an invoice, classification is customer advance and account is 218. Do not call it revenue.",
            "category_code is one code from the list, or empty when none fits (a customer advance has no revenue code).",
            "counterparty and nif are copied from the text. Leave nif empty when the text has no tax id.",
            "confidence is 0-1 for the match. justification is one line.",
            "",
            "Email text:",
            text,
            "",
            "Categories:",
            "\n".join(category_lines) or "(none)",
            "",
            "Candidates:",
            json.dumps(payload, ensure_ascii=False),
        ]
    )
    try:
        return llm_json(prompt, EXPLANATION_SCHEMA)
    except (LLMError, Exception):
        return None


def _store_explanation(session, message, source_id, email_name, amount, proposal, confidence, justification):
    # type: (Session, Any, str, str, Decimal, Dict[str, str], float, str) -> Document
    when = message.date.date() if message.date is not None else date.today()
    period = when.strftime("%Y-%m")
    if session.exec(select(Period).where(Period.month == period)).first() is None:
        session.add(Period(month=period, status="open"))
        session.flush()
    blob = "%s\n%s" % (message.subject or "", message.body or "")
    raw_nif = "".join(ch for ch in (proposal.get("nif") or "") if ch.isdigit())
    kind, cleaned = classify_nif(raw_nif)
    nif = cleaned if kind == "pt" and _nif_in_text(cleaned, blob) else ""
    entity = proposal.get("counterparty") or ""
    if not _name_in_text(entity, blob):
        entity = "unknown"
    notes = " | ".join(
        [
            "kind: explanation",
            "classification: %s" % (proposal.get("classification") or "explanation"),
            "account: %s" % (proposal.get("account") or ""),
            "category_code: %s" % (proposal.get("category_code") or ""),
            "email: %s" % email_name,
            "source_id: %s" % source_id,
        ]
    )
    existing = session.exec(select(Document).where(Document.source_id == source_id)).first()
    fields = {
        "source_id": source_id,
        "period": period,
        "document_date": when,
        "tipo": "explicacao",
        "entity": entity or "unknown",
        "nif": nif or None,
        "document_number": "explanation %s" % email_name,
        "base": to_money(amount),
        "vat_rate": ZERO,
        "vat": ZERO,
        "total": to_money(amount),
        "deductible_vat": ZERO,
        "status": "validated",
        "notes": notes,
        "original_filename": email_name,
        "direction": "sale" if proposal.get("account") == "218" else None,
        "confidence": float(confidence),
        "justification": _one_line(justification),
    }
    if existing is None:
        document = Document(**fields)
        session.add(document)
    else:
        for key, value in fields.items():
            setattr(existing, key, value)
        document = existing
        session.add(document)
    session.flush()
    session.refresh(document)
    return document


def resolve_explanation(session, message, source_id, email_name, categories):
    # type: (Session, Any, str, str, Sequence[Any]) -> Dict[str, Any]
    """Match an email with no attachment to a pending line. Store it as evidence."""
    text = "%s\n%s" % (message.subject or "", message.body or "")
    amounts = _parse_amounts(text)
    dates = _parse_dates(text, message.date)
    if not amounts:
        return {
            "outcome": "none",
            "confidence": 0,
            "justification": "The email states no amount.",
            "email": email_name,
        }
    ranked = []  # type: List[Dict[str, Any]]
    for candidate in _load_candidates(session):
        row = _score_text(text, amounts, dates, candidate)
        if row["confidence"] < 0.4 and not row["signals"]["amount"]:
            continue
        ranked.append(row)
    ranked.sort(key=lambda row: (-row["confidence"], _candidate_key(row["candidate"])))
    if not ranked:
        return {
            "outcome": "none",
            "confidence": 0,
            "justification": "No pending item matches this explanation.",
            "email": email_name,
        }
    chosen = ranked[0]
    reply = _ask_explanation(text, ranked, categories)
    proposal = {
        "classification": "",
        "account": "",
        "category_code": "",
        "counterparty": "",
        "nif": "",
    }
    if reply is not None:
        model_confidence = float(reply.get("confidence") or 0)
        key = (reply.get("candidate_key") or "").strip()
        picked = None  # type: Optional[Dict[str, Any]]
        for row in ranked:
            if key and _candidate_key(row["candidate"]) == key:
                picked = row
                break
        if picked is not None:
            chosen = picked
            if not chosen["signals"]["amount"]:
                chosen["confidence"] = min(model_confidence, 0.89)
            elif _all_clear(chosen["signals"]):
                chosen["confidence"] = max(model_confidence, float(chosen["confidence"]))
            else:
                chosen["confidence"] = model_confidence
        elif not (_all_clear(chosen["signals"]) and not _ambiguous(ranked)):
            chosen["confidence"] = min(float(chosen["confidence"]), 0.49)
        if picked is not None and reply.get("justification"):
            chosen["reason"] = _one_line(reply.get("justification"))
        proposal["classification"] = _one_line(reply.get("classification"))
        proposal["account"] = _one_line(reply.get("account"))
        proposal["category_code"] = _one_line(reply.get("category_code"))
        proposal["counterparty"] = _one_line(reply.get("counterparty"))
        proposal["nif"] = _one_line(reply.get("nif"))
    if _advance_text(text):
        code = proposal["category_code"]
        if not proposal["classification"] or code.startswith("REC_"):
            proposal["classification"] = "customer advance"
        if code.startswith("REC_") or not proposal["account"]:
            proposal["account"] = "218"
            proposal["category_code"] = ""
            chosen["reason"] = _one_line(
                "%s Payment received before the invoice is a customer advance, account 218, not revenue."
                % chosen["reason"]
            )
    confidence = float(chosen["confidence"])
    if confidence < MEDIUM_CONFIDENCE or not chosen["signals"]["amount"]:
        return {
            "outcome": "none",
            "confidence": confidence,
            "justification": chosen["reason"],
            "email": email_name,
        }
    amount = chosen["signals"]["amount_value"] or ZERO
    document = _store_explanation(
        session,
        message,
        source_id,
        email_name,
        amount,
        proposal,
        confidence,
        chosen["reason"],
    )
    document.notes = set_note(
        document.notes, "proposed_classification", proposal["classification"] or "explanation"
    )
    document.notes = set_note(document.notes, "proposed_account", proposal["account"])
    session.add(document)
    candidate = chosen["candidate"]
    item = candidate.get("item")
    line = candidate.get("line")
    if confidence >= AUTO_CONFIDENCE and line is not None:
        linked = _link_amount(
            session,
            document,
            line,
            _line_amount(line),
            "explanation",
            chosen["reason"],
            confidence,
        )
        if linked and item is not None:
            post_id = _mark_resolved(
                session,
                item,
                document,
                line,
                confidence,
                chosen["reason"],
                source_id,
            )
            _stamp_document(document, confidence, chosen["reason"], "linked")
            document.notes = set_note(document.notes, "fully_linked", "yes")
            session.add(document)
            session.commit()
            session.refresh(item)
            history = _history_text(item, document)
            payload = _decision_payload(
                "linked", document, chosen, history, post_id, None, email_name
            )
            payload["method"] = "explanation"
            payload["classification"] = proposal["classification"]
            payload["account"] = proposal["account"]
            return payload
        if not linked:
            session.rollback()
            confidence = min(confidence, 0.85)
            chosen["confidence"] = confidence
            document = _store_explanation(
                session,
                message,
                source_id,
                email_name,
                amount,
                proposal,
                confidence,
                chosen["reason"],
            )
    task = _confirm_task(
        session,
        "confirm_explanation",
        candidate,
        document,
        confidence,
        chosen["reason"],
        email_name,
    )
    task.notes = set_note(task.notes, "classification", proposal["classification"])
    task.notes = set_note(task.notes, "account", proposal["account"])
    session.add(task)
    _stamp_document(document, confidence, chosen["reason"], "confirm")
    session.add(document)
    session.commit()
    payload = _decision_payload(
        "confirm", document, chosen, "", None, task.id, email_name
    )
    payload["method"] = "explanation"
    payload["classification"] = proposal["classification"]
    payload["account"] = proposal["account"]
    return payload


def attach_late_results(session, results, message):
    # type: (Session, List[Dict[str, Any]], Any) -> None
    """Run the pending matcher for every new document in this message."""
    context = "%s\n%s" % (message.subject or "", message.body or "")
    email_name = ""
    source = message.message_id or ""
    for item in results:
        if item.get("email"):
            email_name = item.get("email") or ""
        if item.get("already_existed"):
            continue
        if item.get("status") not in ("validated", "review"):
            continue
        document_id = item.get("document_id")
        if not document_id:
            continue
        document = session.get(Document, document_id)
        if document is None:
            continue
        try:
            decision = resolve_late_document(
                session, document, context, email_name=email_name, email_source=source
            )
        except Exception:
            session.rollback()
            decision = {
                "outcome": "none",
                "document_id": document_id,
                "justification": "Pending match failed.",
                "confidence": 0,
            }
        item["late_match"] = decision
        application = credit_payload_for_invoice(session, document_id)
        if application:
            item["credit_application"] = application


def attach_explanation(session, item, message, source_id, categories):
    # type: (Session, Dict[str, Any], Any, str, Sequence[Any]) -> Dict[str, Any]
    email_name = item.get("email") or ""
    try:
        decision = resolve_explanation(session, message, source_id, email_name, categories)
    except Exception:
        import logging

        logging.getLogger("app.pending").exception("explanation match failed")
        session.rollback()
        decision = {
            "outcome": "none",
            "confidence": 0,
            "justification": "Explanation match failed.",
            "email": email_name,
        }
    item["late_match"] = decision
    if decision.get("outcome") in ("linked", "confirm"):
        item["open_item_id"] = decision.get("confirm_task_id") or item.get("open_item_id")
    return decision


def _negative_sum(document):
    # type: (Document) -> Decimal
    import json

    total = ZERO
    if not document.lines_json:
        return total
    try:
        rows = json.loads(document.lines_json)
    except (TypeError, ValueError):
        return total
    if not isinstance(rows, list):
        return total
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            amount = to_money(row.get("total") or "0")
        except Exception:
            continue
        if amount < ZERO:
            total = to_money(total + (-amount))
    return total


def _is_credit(document):
    # type: (Document) -> bool
    if document.total is not None and document.total < ZERO:
        return True
    number = (document.document_number or "").upper()
    if number.startswith("NC"):
        return True
    notes = (document.notes or "").lower()
    return "credit note" in notes or "nota de crédito" in notes or "nota de credito" in notes


def _source_mentions(document, token):
    # type: (Document, str) -> bool
    """True when the stored text or the PDF itself names this credit note."""
    if not token:
        return False
    blob = _compact(
        " ".join(
            [
                document.lines_json or "",
                document.notes or "",
                document.referenced_invoice or "",
                document.document_number or "",
            ]
        )
    )
    if token in blob:
        return True
    raw_path = document.file_path or ""
    if not raw_path:
        return False
    from pathlib import Path

    from app.db import ROOT

    path = Path(raw_path)
    if not path.is_absolute():
        path = ROOT / raw_path
    if not path.exists():
        return False
    try:
        from app.extract import pdf_text

        text = pdf_text(path.read_bytes())
    except Exception:
        return False
    return token in _compact(text)


def _already_netted(invoice, credit):
    # type: (Document, Document) -> bool
    """The printed invoice already deducts the credit, so do not subtract it twice."""
    number = _compact(credit.document_number or "")
    deducted = _negative_sum(invoice)
    credit_abs = to_money(abs(credit.total))
    credit_base = to_money(abs(credit.base or ZERO))
    if money_equal(deducted, credit_abs) or (
        credit_base > ZERO and money_equal(deducted, credit_base)
    ):
        return True
    return _source_mentions(invoice, number)


def apply_open_credits(session):
    # type: (Session) -> List[CreditApplication]
    """Deduct each unapplied credit note from the issuer's next invoice."""
    created = []  # type: List[CreditApplication]
    credits = session.exec(select(Document).order_by(Document.document_date, Document.id)).all()
    for credit in credits:
        if not _is_credit(credit) or credit.id is None:
            continue
        if (note_value(credit.notes, "credit_applies") or "") != "next_invoice":
            continue
        if note_value(credit.notes, "applied_to"):
            continue
        existing = session.exec(
            select(CreditApplication).where(CreditApplication.credit_document_id == credit.id)
        ).first()
        if existing is not None:
            continue
        nif = "".join(ch for ch in (credit.nif or "") if ch.isdigit())
        if not nif:
            continue
        credit_abs = to_money(abs(credit.total))
        if credit_abs <= ZERO:
            continue
        chosen = None  # type: Optional[Document]
        for invoice in credits:
            if invoice.id == credit.id or invoice.status == "duplicate":
                continue
            if _is_credit(invoice):
                continue
            if invoice.total is None or invoice.total <= ZERO:
                continue
            other = "".join(ch for ch in (invoice.nif or "") if ch.isdigit())
            if other != nif:
                continue
            if credit.referenced_invoice and _compact(invoice.document_number) == _compact(
                credit.referenced_invoice
            ):
                continue
            if invoice.document_date < credit.document_date:
                continue
            if chosen is None or (invoice.document_date, invoice.id or 0) < (
                chosen.document_date,
                chosen.id or 0,
            ):
                chosen = invoice
        if chosen is None or chosen.id is None:
            continue
        netted = _already_netted(chosen, credit)
        if netted:
            payable = to_money(chosen.total)
            gross = to_money(payable + credit_abs)
        else:
            gross = to_money(chosen.total)
            payable = to_money(gross - credit_abs)
        if payable < ZERO:
            continue
        explanation = _one_line(
            "Credit note %s of %s from %s is deducted from %s. Amount payable is %s (%s minus %s)."
            % (
                credit.document_number,
                credit_abs,
                credit.entity,
                chosen.document_number,
                payable,
                gross,
                credit_abs,
            )
        )
        now = datetime.utcnow()
        application = CreditApplication(
            credit_document_id=credit.id,
            invoice_document_id=chosen.id,
            invoice_total=gross,
            credit_amount=credit_abs,
            amount_payable=payable,
            applied_at=now,
            explanation=explanation,
        )
        session.add(application)
        credit.notes = set_note(credit.notes, "applied_to", chosen.document_number or "")
        credit.notes = set_note(credit.notes, "applied_amount", str(credit_abs))
        chosen.notes = set_note(chosen.notes, "credit_applied", credit.document_number or "")
        chosen.notes = set_note(chosen.notes, "credit_applied_amount", str(credit_abs))
        chosen.notes = set_note(chosen.notes, "amount_payable", str(payable))
        chosen.notes = set_note(chosen.notes, "cash_amount", str(payable))
        session.add(credit)
        session.add(chosen)
        marker = "credit_balance: %s" % credit.id
        for item in session.exec(select(OpenItem).where(OpenItem.document_id == credit.id)).all():
            if item.notes and marker in item.notes:
                item.status = "done"
                item.notes = set_note(item.notes, "applied_to", chosen.document_number or "")
                item.justification = explanation
                session.add(item)
        created.append(application)
    if created:
        session.commit()
    return created


def _application_row(session, application):
    # type: (Session, CreditApplication) -> Dict[str, Any]
    credit = session.get(Document, application.credit_document_id)
    invoice = session.get(Document, application.invoice_document_id)
    return {
        "id": application.id,
        "credit_document_id": application.credit_document_id,
        "credit_note": credit.document_number if credit is not None else None,
        "invoice_document_id": application.invoice_document_id,
        "invoice": invoice.document_number if invoice is not None else None,
        "issuer": invoice.entity if invoice is not None else None,
        "invoice_total": str(to_money(application.invoice_total)),
        "credit": str(to_money(application.credit_amount)),
        "amount_payable": str(to_money(application.amount_payable)),
        "date": application.applied_at.date().isoformat() if application.applied_at else None,
        "explanation": application.explanation,
    }


def credit_payload_for_invoice(session, invoice_id):
    # type: (Session, Optional[int]) -> Optional[Dict[str, Any]]
    if not invoice_id:
        return None
    row = session.exec(
        select(CreditApplication).where(CreditApplication.invoice_document_id == invoice_id)
    ).first()
    if row is None:
        return None
    return _application_row(session, row)


def credit_applications_for_month(session, month):
    # type: (Session, str) -> List[Dict[str, Any]]
    rows = []  # type: List[Dict[str, Any]]
    for application in session.exec(select(CreditApplication).order_by(CreditApplication.id)).all():
        credit = session.get(Document, application.credit_document_id)
        invoice = session.get(Document, application.invoice_document_id)
        periods = set()
        if credit is not None:
            periods.add(credit.period)
        if invoice is not None:
            periods.add(invoice.period)
        if month not in periods:
            continue
        rows.append(_application_row(session, application))
    return rows


def resolved_after_close(session, month):
    # type: (Session, str) -> List[Dict[str, Any]]
    rows = session.exec(
        select(PostCloseResolution)
        .where(PostCloseResolution.period == month)
        .order_by(PostCloseResolution.id)
    ).all()
    payload = []  # type: List[Dict[str, Any]]
    for row in rows:
        item = session.get(OpenItem, row.open_item_id) if row.open_item_id else None
        document = session.get(Document, row.document_id) if row.document_id else None
        accepted_date = None
        if item is not None and item.approved_at is not None:
            accepted_date = item.approved_at.date().isoformat()
        payload.append(
            {
                "id": row.id,
                "open_item_id": row.open_item_id,
                "kind": item.kind if item is not None else None,
                "item": item.description if item is not None else row.summary,
                "amount": str(to_money(item.amount)) if item is not None else None,
                "status": item.status if item is not None else RESOLVED_STATUS,
                "bank_line_id": row.bank_line_id,
                "document_id": row.document_id,
                "document_number": document.document_number if document is not None else None,
                "email_source_id": row.email_source_id,
                "date": row.resolved_at.date().isoformat() if row.resolved_at else None,
                "summary": row.summary,
                "replaces": {
                    "status": "accepted_by_owner",
                    "reason": item.approval_reason if item is not None else None,
                    "approved_by": item.approved_by if item is not None else None,
                    "date": accepted_date,
                },
            }
        )
    return payload


def format_late_match(item):
    # type: (Dict[str, Any]) -> str
    """One block the demonstration prints for an email or a scan."""
    match = item.get("late_match") or {}
    lines = [
        "email: %s" % (item.get("email") or ""),
        "attachment: %s" % (item.get("attachment") or "-"),
        "outcome: %s" % (match.get("outcome") or "none"),
    ]
    if match.get("open_item_id") or match.get("item"):
        lines.append(
            "item: %s | %s | %s"
            % (match.get("open_item_id"), match.get("item_kind") or "", match.get("item") or "")
        )
    if match.get("bank_line_id"):
        lines.append("bank_line_id: %s" % match.get("bank_line_id"))
    if match.get("document_id") or match.get("document_number"):
        lines.append(
            "document: %s | %s"
            % (match.get("document_id"), match.get("document_number") or "")
        )
    if match.get("email") and match.get("method") == "explanation":
        lines.append("explanation_email: %s" % match.get("email"))
    if "confidence" in match:
        lines.append("confidence: %s" % match.get("confidence"))
    if match.get("justification"):
        lines.append("justification: %s" % match.get("justification"))
    if match.get("classification") or match.get("account"):
        lines.append(
            "classification: %s | account %s"
            % (match.get("classification") or "", match.get("account") or "")
        )
    if match.get("history"):
        lines.append("history: %s" % match.get("history"))
    if match.get("post_close_id"):
        lines.append("post_close_resolution: %s" % match.get("post_close_id"))
    if match.get("confirm_task_id"):
        lines.append("confirm_task: %s" % match.get("confirm_task_id"))
    credit = item.get("credit_application")
    if credit:
        lines.append(
            "credit_application: %s on %s | invoice_total %s | credit %s | payable %s"
            % (
                credit.get("credit_note"),
                credit.get("invoice"),
                credit.get("invoice_total"),
                credit.get("credit"),
                credit.get("amount_payable"),
            )
        )
    return "\n".join(lines)

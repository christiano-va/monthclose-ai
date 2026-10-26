"""Attach every document that supports a bank line.

The first passes keep a single paying document. This pass completes the set:
platform payouts, settlements, expense notes and partial receipts.
A zero allocation with method "evidence" supports the line without carrying
its amount. The signed allocations of one line still sum to the line amount.
"""
import json
import logging
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlmodel import Session, select

from app.linker import (
    _BOOKING_RE,
    _compact,
    _kind,
    bank_in,
    cash_amount,
    name_overlap,
    reference_tokens,
    within_days,
)
from app.models import BankLine, Category, Document, Link, OpenItem, Period
from app.money import ZERO, money_equal, to_money
from app.posting import (
    TIPO_RECEITA,
    excluded_from_linking,
    find_referenced_invoices,
    note_value,
)

logger = logging.getLogger("app.link")

_SETTLEMENT_KINDS = ("receipt", "payment_notice", "payment_proof")
_PAYMENT_KINDS = ("receipt", "payment_proof")
_GUEST_RE = re.compile(
    r"^(?P<code>[A-Z]{2,}\d[A-Z0-9]{2,})\s+(?P<guest>.+?)\s+\d{1,2}\s*[-–]\s*\d{1,2}/",
    re.IGNORECASE,
)


def links_support_line(line, links):
    # type: (BankLine, Sequence[Link]) -> bool
    """True when the signed allocations equal the absolute bank amount."""
    if not links:
        return False
    total = ZERO
    for link in links:
        total = to_money(total + to_money(link.allocated_amount))
    return money_equal(total, abs(line.amount))


def _one_line(text):
    # type: (str) -> str
    return " ".join((text or "").split())


def _rows(document):
    # type: (Document) -> List[Dict[str, Any]]
    if not document.lines_json:
        return []
    try:
        parsed = json.loads(document.lines_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    rows = []  # type: List[Dict[str, Any]]
    for row in parsed:
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _row_amount(row):
    # type: (Dict[str, Any]) -> Decimal
    return to_money(abs(to_money(row.get("total") or "0")))


def _usable_documents(session):
    # type: (Session) -> List[Document]
    categories = {}  # type: Dict[int, str]
    for category in session.exec(select(Category)).all():
        if category.id is not None:
            categories[category.id] = category.code
    found = []  # type: List[Document]
    for document in session.exec(select(Document).order_by(Document.id)).all():
        if document.status in ("duplicate", "error", "no_document"):
            continue
        code = categories.get(document.category_id) if document.category_id else None
        if excluded_from_linking(code, document.notes, document.status):
            continue
        found.append(document)
    return found


def _links_by_line(session):
    # type: (Session) -> Dict[int, List[Link]]
    grouped = {}  # type: Dict[int, List[Link]]
    for link in session.exec(select(Link)).all():
        grouped.setdefault(link.bank_line_id, []).append(link)
    return grouped


def _apply_plan(session, line, entries):
    # type: (Session, BankLine, List[Tuple[Document, Decimal, str, str, float]]) -> bool
    """Write the link set when, and only when, it sums to the bank line."""
    if line.id is None:
        return False
    existing = session.exec(select(Link).where(Link.bank_line_id == line.id)).all()
    by_doc = {}  # type: Dict[int, Link]
    for link in existing:
        by_doc[link.document_id] = link
    planned = {}  # type: Dict[int, Tuple[Document, Decimal, str, str, float]]
    for document, amount, method, explanation, confidence in entries:
        if document.id is None:
            return False
        amount = to_money(amount)
        if amount == ZERO:
            method = "evidence"
        planned[document.id] = (document, amount, method, _one_line(explanation), confidence)
    total = ZERO
    for link in existing:
        if link.document_id in planned:
            continue
        total = to_money(total + to_money(link.allocated_amount))
    for document_id in planned:
        total = to_money(total + planned[document_id][1])
    if not money_equal(total, abs(line.amount)):
        logger.warning(
            "skip line %s: planned allocations %s do not sum to %s",
            line.id,
            total,
            to_money(abs(line.amount)),
        )
        return False
    for document_id, (document, amount, method, explanation, confidence) in planned.items():
        current = by_doc.get(document_id)
        if (
            current is not None
            and amount > ZERO
            and money_equal(current.allocated_amount, amount)
        ):
            method = current.method
            confidence = current.confidence
        if current is None:
            current = Link(
                document_id=document.id or 0,
                bank_line_id=line.id,
                allocated_amount=amount,
                method=method,
                explanation=explanation,
                confidence=float(confidence),
            )
        else:
            current.allocated_amount = amount
            current.method = method
            current.explanation = explanation
            current.confidence = float(confidence)
        session.add(current)
    line.status = "linked"
    session.add(line)
    return True


def _booking_rows(report):
    # type: (Document) -> List[Dict[str, Any]]
    bookings = []  # type: List[Dict[str, Any]]
    for row in _rows(report):
        if (row.get("nature") or "") == "fee":
            continue
        gross = _row_amount(row)
        if gross <= ZERO:
            continue
        description = str(row.get("description") or "").strip()
        match = _GUEST_RE.search(description)
        code = ""
        guest = ""
        if match:
            code = match.group("code").upper()
            guest = " ".join(match.group("guest").split())
        else:
            found = _BOOKING_RE.search(description.upper())
            if found:
                code = found.group(1).upper()
                guest = description
        if not code:
            continue
        bookings.append(
            {"code": code, "guest": guest, "gross": gross, "description": description}
        )
    return bookings


def _commission_docs(report, documents, codes):
    # type: (Document, Sequence[Document], Sequence[str]) -> List[Document]
    tokens = []  # type: List[str]
    for token in re.findall(r"[A-Za-zÀ-ÿ]{4,}", report.entity or ""):
        lowered = token.lower()
        if lowered not in tokens and lowered not in ("ltd", "limited", "ireland"):
            tokens.append(lowered)
    found = []  # type: List[Document]
    for document in documents:
        if document.id == report.id or _kind(document) == "payout_report":
            continue
        blob = (document.lines_json or "").upper()
        if not any(code in blob for code in codes):
            continue
        entity = (document.entity or "").lower()
        if tokens and not any(token in entity for token in tokens):
            continue
        found.append(document)
    return found


def _fee_for(code, commission_docs):
    # type: (str, Sequence[Document]) -> Tuple[Optional[Document], Optional[Decimal]]
    fallback = None  # type: Optional[Tuple[Document, Decimal]]
    for document in commission_docs:
        for row in _rows(document):
            if code not in str(row.get("description") or "").upper():
                continue
            amount = _row_amount(row)
            if amount <= ZERO:
                continue
            if (row.get("nature") or "") == "fee":
                return document, amount
            if fallback is None:
                fallback = (document, amount)
    if fallback is None:
        return None, None
    return fallback


def _sales_invoice(documents, guest, gross, skip_ids):
    # type: (Sequence[Document], str, Decimal, Sequence[int]) -> Optional[Document]
    tokens = re.findall(r"[A-Za-zÀ-ÿ]{4,}", guest or "")
    if not tokens:
        return None
    hits = []  # type: List[Tuple[int, Document]]
    for document in documents:
        if document.id in skip_ids:
            continue
        if document.status in ("duplicate", "error", "no_document", "pago", "recebido"):
            continue
        if document.tipo != TIPO_RECEITA and document.direction != "sale":
            continue
        if _kind(document) in _SETTLEMENT_KINDS or _kind(document) == "payout_report":
            continue
        if not money_equal(abs(document.total), gross):
            continue
        entity = document.entity or ""
        score = 0
        for token in tokens:
            if token.lower() in entity.lower():
                score += 1
        if score == 0:
            continue
        hits.append((score, document))
    if not hits:
        return None
    hits.sort(
        key=lambda item: (
            -item[0],
            0 if (item[1].justification or "") != "loaded from data/history JSON" else 1,
            item[1].id or 0,
        )
    )
    if len(hits) == 1 or hits[0][0] > hits[1][0]:
        return hits[0][1]
    inbox = [
        item
        for item in hits
        if (item[1].justification or "") != "loaded from data/history JSON"
    ]
    if len(inbox) == 1 or (len(inbox) > 1 and inbox[0][0] > inbox[1][0]):
        return inbox[0][1]
    return None


def _line_for_booking(lines, code, net):
    # type: (Sequence[BankLine], str, Decimal) -> Optional[BankLine]
    hits = []  # type: List[BankLine]
    for line in lines:
        if not bank_in(line):
            continue
        description = (line.description or "").upper()
        if code not in description and code not in _compact(line.description):
            continue
        hits.append(line)
    matched = [line for line in hits if money_equal(abs(line.amount), net)]
    if len(matched) == 1:
        return matched[0]
    if len(hits) == 1:
        return hits[0]
    return None


def _tasks_for_booking(session, code):
    # type: (Session, str) -> List[OpenItem]
    needle_booking = "booking: %s" % code
    needle_reference = "reference: %s" % code
    found = []  # type: List[OpenItem]
    rows = session.exec(select(OpenItem)).all()
    for item in rows:
        if item.kind not in ("missing_document", "missing_sales_invoice"):
            continue
        notes = item.notes or ""
        if needle_booking in notes or needle_reference in notes:
            found.append(item)
    found.sort(key=lambda item: item.id or 0)
    return found


def _upsert_missing_sale(session, line, report, code, guest, gross):
    # type: (Session, BankLine, Document, str, str, Decimal) -> None
    matches = _tasks_for_booking(session, code)
    description = (
        "Owner: missing sales invoice for guest %s, booking %s, gross %s."
        % (guest or "unknown", code, to_money(gross))
    )
    email = None  # type: Optional[str]
    if matches:
        email = note_value(matches[0].notes, "email")
    notes = [
        "linker: missing_sales_invoice",
        "booking: %s" % code,
        "guest: %s" % (guest or "unknown"),
        "gross: %s" % to_money(gross),
        "bank_line_id: %s" % line.id,
        "payout_document_id: %s" % report.id,
    ]
    if email:
        notes.append("email: %s" % email)
    note_text = " | ".join(notes)
    primary = matches[0] if matches else None
    if primary is None:
        if session.exec(select(Period).where(Period.month == line.period)).first() is None:
            session.add(Period(month=line.period, status="open"))
            session.flush()
        primary = OpenItem(
            document_id=report.id,
            period=line.period,
            kind="missing_sales_invoice",
            description=description,
            amount=to_money(gross),
            due_date=None,
            status="open",
            notes=note_text,
            assignee="owner",
            confidence=0.9,
            justification="Payout booking %s has no sales invoice in the mailbox." % code,
        )
        session.add(primary)
    else:
        primary.kind = "missing_sales_invoice"
        primary.description = description
        primary.amount = to_money(gross)
        primary.period = line.period
        primary.document_id = report.id
        primary.assignee = "owner"
        primary.notes = note_text
        primary.confidence = 0.9
        primary.justification = (
            "Payout booking %s has no sales invoice in the mailbox." % code
        )
        if primary.status != "accepted_by_owner":
            primary.status = "open"
        session.add(primary)
    for extra in matches[1:]:
        if extra.status == "open":
            extra.status = "done"
            extra.notes = (extra.notes or "") + " | superseded_by: missing_sales_invoice"
            session.add(extra)


def _close_sale_task(session, code):
    # type: (Session, str) -> None
    for item in _tasks_for_booking(session, code):
        if item.status == "open":
            item.status = "done"
            item.justification = "Sales invoice for booking %s is now linked." % code
            session.add(item)


def _complete_payouts(session, documents, lines):
    # type: (Session, Sequence[Document], Sequence[BankLine]) -> int
    written = 0
    reports = [document for document in documents if _kind(document) == "payout_report"]
    for report in reports:
        bookings = _booking_rows(report)
        if not bookings:
            continue
        codes = [item["code"] for item in bookings]
        commissions = _commission_docs(report, documents, codes)
        for booking in bookings:
            code = booking["code"]
            guest = booking["guest"]
            gross = booking["gross"]
            commission, fee = _fee_for(code, commissions)
            if commission is None or fee is None:
                logger.info("payout %s has no commission line", code)
                continue
            net = to_money(gross - fee)
            line = _line_for_booking(lines, code, net)
            if line is None or not money_equal(abs(line.amount), net):
                logger.info(
                    "payout %s net %s does not match a bank line", code, net
                )
                continue
            skip_ids = [report.id or 0, commission.id or 0]
            sale = _sales_invoice(documents, guest, gross, skip_ids)
            if sale is not None and not money_equal(abs(sale.total), gross):
                sale = None
            entries = []  # type: List[Tuple[Document, Decimal, str, str, float]]
            if sale is not None:
                entries.append(
                    (
                        sale,
                        gross,
                        "payout",
                        "Sales invoice %s for %s is the gross %s of booking %s."
                        % (sale.document_number, guest or sale.entity, gross, code),
                        0.97,
                    )
                )
                entries.append(
                    (
                        report,
                        ZERO,
                        "evidence",
                        "Payout report supports booking %s; gross %s is on %s and commission %s is deducted."
                        % (code, gross, sale.document_number, fee),
                        0.97,
                    )
                )
            else:
                entries.append(
                    (
                        report,
                        gross,
                        "payout",
                        "Booking %s gross %s (%s) has no sales invoice, so the payout report carries the gross."
                        % (code, gross, guest or "unknown guest"),
                        0.9,
                    )
                )
            entries.append(
                (
                    commission,
                    to_money(-fee),
                    "payout",
                    "Platform commission %s for booking %s is deducted from gross %s; the bank credit is %s."
                    % (fee, code, gross, net),
                    0.97,
                )
            )
            if _apply_plan(session, line, entries):
                written += 1
                if sale is not None:
                    _close_sale_task(session, code)
                else:
                    _upsert_missing_sale(session, line, report, code, guest, gross)
    return written


def _is_expense_note(document):
    # type: (Document) -> bool
    if (note_value(document.notes, "normal_reimbursement") or "") == "yes":
        return True
    return _kind(document) == "other" and "reembolso" in (document.notes or "").lower()


def _expense_documents(note, documents):
    # type: (Document, Sequence[Document]) -> List[Tuple[Document, Decimal]]
    found = []  # type: List[Tuple[Document, Decimal]]
    seen = set()  # type: set
    for row in _rows(note):
        amount = _row_amount(row)
        if amount <= ZERO:
            continue
        description = str(row.get("description") or "")
        numbers = find_referenced_invoices(None, description, note.document_number or "")
        if not numbers:
            continue
        for number in numbers.split(","):
            number = number.strip()
            if not number:
                continue
            match = _find_by_number(documents, number)
            if match is None or match.id == note.id or match.id in seen:
                continue
            if not money_equal(abs(match.total), amount):
                continue
            seen.add(match.id)
            found.append((match, amount))
    return found


def _find_by_number(documents, number):
    # type: (Sequence[Document], str) -> Optional[Document]
    wanted = " ".join((number or "").split()).lower()
    exact = []  # type: List[Document]
    for document in documents:
        current = " ".join((document.document_number or "").split()).lower()
        if current == wanted:
            exact.append(document)
    if len(exact) == 1:
        return exact[0]
    token = _compact(number)
    if len(token) < 4:
        return None
    compact_hits = [
        document for document in documents if _compact(document.document_number) == token
    ]
    if len(compact_hits) == 1:
        return compact_hits[0]
    return None


def _line_for_note(note, lines, links_by_line):
    # type: (Document, Sequence[BankLine], Dict[int, List[Link]]) -> Optional[BankLine]
    amount = cash_amount(note)
    if amount is None:
        amount = to_money(abs(note.total))
    for line in lines:
        for link in links_by_line.get(line.id or 0, []):
            if link.document_id == note.id:
                return line
    hits = []  # type: List[BankLine]
    for line in lines:
        if bank_in(line):
            continue
        if not money_equal(abs(line.amount), amount):
            continue
        if not within_days(line, note, 21):
            continue
        if not name_overlap(note.entity, line.description):
            continue
        hits.append(line)
    if len(hits) == 1:
        return hits[0]
    return None


def _complete_reimbursements(session, documents, lines):
    # type: (Session, Sequence[Document], Sequence[BankLine]) -> int
    written = 0
    links_by_line = _links_by_line(session)
    for note in documents:
        if not _is_expense_note(note):
            continue
        expenses = _expense_documents(note, documents)
        if len(expenses) < 2:
            continue
        total = ZERO
        for _document, amount in expenses:
            total = to_money(total + amount)
        if not money_equal(total, to_money(abs(note.total))):
            logger.info(
                "expense note %s lines %s do not sum to %s",
                note.document_number,
                total,
                note.total,
            )
            continue
        line = _line_for_note(note, lines, links_by_line)
        if line is None or not money_equal(abs(line.amount), total):
            continue
        detail = ", ".join(
            "%s %s" % (document.document_number, amount) for document, amount in expenses
        )
        entries = []  # type: List[Tuple[Document, Decimal, str, str, float]]
        for document, amount in expenses:
            entries.append(
                (
                    document,
                    amount,
                    "reimbursement",
                    "%s %s is an expense on note %s; together the expenses are %s."
                    % (document.document_number, amount, note.document_number, total),
                    0.95,
                )
            )
        entries.append(
            (
                note,
                ZERO,
                "evidence",
                "Expense note %s lists %s, which sum to the reimbursement %s."
                % (note.document_number, detail, total),
                0.95,
            )
        )
        if _apply_plan(session, line, entries):
            written += 1
    return written


def _reference_numbers(document):
    # type: (Document) -> List[str]
    numbers = []  # type: List[str]
    raw = document.referenced_invoice or ""
    for part in raw.split(","):
        part = part.strip()
        if part and part not in numbers:
            numbers.append(part)
    guide = note_value(document.notes, "settles_guide") or ""
    if guide and guide not in numbers:
        numbers.append(guide)
    return numbers


def _settlement_targets(document, documents):
    # type: (Document, Sequence[Document]) -> List[Document]
    if _kind(document) not in _SETTLEMENT_KINDS:
        return []
    found = []  # type: List[Document]
    for number in _reference_numbers(document):
        match = _find_by_number(documents, number)
        if match is None or match.id == document.id:
            continue
        if match not in found:
            found.append(match)
    return found


def _expect_credit(settlement, settled):
    # type: (Document, Document) -> bool
    if _kind(settlement) == "payment_proof":
        return False
    if settled.tipo == TIPO_RECEITA or settled.direction == "sale":
        return True
    return False


def _line_locked(line, links, related_ids):
    # type: (BankLine, Sequence[Link], Sequence[int]) -> bool
    if not links:
        return False
    for link in links:
        if link.document_id in related_ids:
            return False
        if link.method in ("payout", "reimbursement"):
            return True
    if links_support_line(line, links):
        return True
    return False


def _desc_tokens(document):
    # type: (Document) -> List[str]
    tokens = list(reference_tokens(document))
    for raw in _reference_numbers(document):
        token = _compact(raw)
        if len(token) >= 6 and token not in tokens:
            tokens.append(token)
    own = _compact(document.document_number or "")
    if len(own) >= 6 and own not in tokens:
        tokens.append(own)
    return tokens


def _pick_line(settlement, settled, lines, links_by_line):
    # type: (Document, Document, Sequence[BankLine], Dict[int, List[Link]]) -> Optional[BankLine]
    related = [settlement.id or 0, settled.id or 0]
    for line in lines:
        for link in links_by_line.get(line.id or 0, []):
            if link.document_id == settlement.id:
                return line
    expect_credit = _expect_credit(settlement, settled)
    tokens = _desc_tokens(settlement) + _desc_tokens(settled)
    amount = cash_amount(settlement)
    if amount is None:
        amount = to_money(abs(settlement.total))
    desc_hits = []  # type: List[BankLine]
    amount_hits = []  # type: List[BankLine]
    for line in lines:
        links = links_by_line.get(line.id or 0, [])
        if _line_locked(line, links, related):
            continue
        if bank_in(line) != expect_credit:
            continue
        description = _compact(line.description)
        if any(token in description for token in tokens):
            desc_hits.append(line)
        if money_equal(abs(line.amount), amount) and within_days(line, settlement, 45):
            amount_hits.append(line)
    if len(desc_hits) == 1:
        return desc_hits[0]
    if len(amount_hits) == 1:
        return amount_hits[0]
    named = []  # type: List[BankLine]
    for line in amount_hits:
        if name_overlap(settlement.entity, line.description) or name_overlap(
            settled.entity, line.description
        ):
            named.append(line)
    if len(named) == 1:
        return named[0]
    return None


def _running_open(settled, payments):
    # type: (Document, Sequence[Document]) -> Dict[int, Decimal]
    ordered = sorted(payments, key=lambda document: (document.document_date, document.id or 0))
    remaining = to_money(abs(settled.total))
    after = {}  # type: Dict[int, Decimal]
    for payment in ordered:
        remaining = to_money(remaining - to_money(abs(payment.total)))
        if payment.id is not None:
            after[payment.id] = remaining
    return after


def _complete_settlements(session, documents, lines):
    # type: (Session, Sequence[Document], Sequence[BankLine]) -> int
    written = 0
    groups = {}  # type: Dict[int, Dict[str, Any]]
    for document in documents:
        for target in _settlement_targets(document, documents):
            if target.id is None:
                continue
            bucket = groups.setdefault(
                target.id, {"settled": target, "settlements": []}
            )  # type: Dict[str, Any]
            if document not in bucket["settlements"]:
                bucket["settlements"].append(document)
    links_by_line = _links_by_line(session)
    for bucket in groups.values():
        settled = bucket["settled"]
        settlements = bucket["settlements"]  # type: List[Document]
        assignments = {}  # type: Dict[int, List[Document]]
        line_of = {}  # type: Dict[int, BankLine]
        for settlement in settlements:
            line = _pick_line(settlement, settled, lines, links_by_line)
            if line is None or line.id is None:
                continue
            assignments.setdefault(line.id, []).append(settlement)
            line_of[line.id] = line
        payments = [
            document for document in settlements if _kind(document) in _PAYMENT_KINDS
        ]
        remaining_after = _running_open(settled, payments)
        portions = {}  # type: Dict[int, Decimal]
        for line_id, docs in assignments.items():
            payers = [document for document in docs if _kind(document) in _PAYMENT_KINDS]
            if not payers:
                payers = list(docs)
            if len(payers) == 1 and money_equal(to_money(abs(payers[0].total)), to_money(abs(settled.total))):
                portions[line_id] = to_money(abs(line_of[line_id].amount))
            else:
                portion = ZERO
                for payer in payers:
                    portion = to_money(portion + to_money(abs(payer.total)))
                portions[line_id] = portion
        allocated = ZERO
        for portion in portions.values():
            allocated = to_money(allocated + portion)
        if allocated - to_money(abs(settled.total)) > Decimal("0.01"):
            logger.warning(
                "settlement of %s would allocate %s against %s",
                settled.document_number,
                allocated,
                settled.total,
            )
            continue
        for line_id, docs in assignments.items():
            line = line_of[line_id]
            portion = portions[line_id]
            payers = [document for document in docs if _kind(document) in _PAYMENT_KINDS]
            anchor = None  # type: Optional[Document]
            if payers:
                anchor = sorted(payers, key=lambda document: (document.document_date, document.id or 0))[-1]
            remaining = remaining_after.get(anchor.id) if anchor is not None and anchor.id is not None else None
            if remaining is None:
                remaining = to_money(abs(settled.total) - portion)
            names = ", ".join(
                "%s %s" % (_kind(document) or document.tipo, document.document_number)
                for document in docs
            )
            carrier = (
                "%s carries %s settled by %s; remaining open %s."
                % (settled.document_number, portion, names, remaining)
            )
            entries = [
                (
                    settled,
                    portion,
                    "settlement",
                    carrier,
                    0.95,
                )
            ]  # type: List[Tuple[Document, Decimal, str, str, float]]
            for document in docs:
                entries.append(
                    (
                        document,
                        ZERO,
                        "evidence",
                        "%s %s supports %s; the settled document carries %s and remaining open is %s."
                        % (
                            _kind(document) or "document",
                            document.document_number,
                            settled.document_number,
                            portion,
                            remaining,
                        ),
                        0.95,
                    )
                )
            if _apply_plan(session, line, entries):
                written += 1
                links_by_line = _links_by_line(session)
    return written


def partial_invoice_balances(session):
    # type: (Session) -> List[Dict[str, Any]]
    """Running balance of invoices that have a receipt for less than the total."""
    documents = _usable_documents(session)
    grouped = {}  # type: Dict[int, List[Document]]
    settled_of = {}  # type: Dict[int, Document]
    for document in documents:
        if _kind(document) != "receipt":
            continue
        for target in _settlement_targets(document, documents):
            if target.id is None:
                continue
            if money_equal(to_money(abs(document.total)), to_money(abs(target.total))):
                continue
            grouped.setdefault(target.id, []).append(document)
            settled_of[target.id] = target
    rows = []  # type: List[Dict[str, Any]]
    for invoice_id in sorted(grouped):
        invoice = settled_of[invoice_id]
        receipts = sorted(
            grouped[invoice_id],
            key=lambda document: (document.document_date, document.id or 0),
        )
        remaining = to_money(abs(invoice.total))
        steps = []  # type: List[Dict[str, str]]
        for receipt in receipts:
            remaining = to_money(remaining - to_money(abs(receipt.total)))
            steps.append(
                {
                    "document_number": receipt.document_number,
                    "date": receipt.document_date.isoformat(),
                    "amount": str(to_money(abs(receipt.total))),
                    "remaining_open": str(remaining),
                }
            )
        rows.append(
            {
                "document_id": invoice.id,
                "document_number": invoice.document_number,
                "entity": invoice.entity,
                "invoice_total": str(to_money(abs(invoice.total))),
                "remaining_open": str(remaining),
                "receipts": steps,
            }
        )
    return rows


def complete_link_sets(session):
    # type: (Session) -> Dict[str, int]
    """Idempotent. Rewrites a line only when the finished set sums to it."""
    documents = _usable_documents(session)
    lines = session.exec(
        select(BankLine).order_by(BankLine.movement_date, BankLine.id)
    ).all()
    payouts = _complete_payouts(session, documents, lines)
    session.flush()
    reimbursements = _complete_reimbursements(session, documents, lines)
    session.flush()
    settlements = _complete_settlements(session, documents, lines)
    session.flush()
    return {
        "payout": payouts,
        "reimbursement": reimbursements,
        "settlement": settlements,
    }

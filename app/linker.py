"""Link each bank line to the documents that support it.

Deterministic passes run first (reference, exact amount, N:1, 1:N).
What is left goes to the model. A completion pass then attaches every
document that supports the line (payout, settlement, reimbursement).
Code checks that every proposal sums to the line.
"""
import json
import logging
import re
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlmodel import Session, select

from app.llm import LLMError, llm_json
from app.models import BankLine, Category, Document, Link, OpenItem, Period
from app.money import ZERO, money_equal, to_money
from app.posting import (
    TIPO_ESTADO,
    TIPO_GASTO,
    TIPO_INVESTIMENTO,
    TIPO_PESSOAL,
    TIPO_RECEITA,
    TIPO_SETTLEMENT,
    excluded_from_linking,
    norm_nif,
    note_value,
    risk_flag_names,
    suspect_flags,
)
from app.validate import parse_iso_date

logger = logging.getLogger("app.link")

DATE_WINDOW_DAYS = 10
AMOUNT_TOLERANCE = Decimal("0.01")

_NAME_STOP = {
    "lda",
    "ltd",
    "unipessoal",
    "limitada",
    "companhia",
    "seguros",
    "sa",
    "the",
    "and",
    "para",
    "com",
    "dos",
    "das",
    "uma",
    "uns",
}

_FX_RE = re.compile(
    r"(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})\s*([A-Za-z]{3})\s+CAMBIO\s+(\d+[.,]\d+)",
    re.IGNORECASE,
)

_BOOKING_RE = re.compile(r"\b([A-Z]{2,}\d[A-Z0-9]{2,})\b")

LINK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["allocations", "confidence", "explanation"],
    "properties": {
        "allocations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["document_id", "allocated_amount", "explanation"],
                "properties": {
                    "document_id": {"type": "integer"},
                    "allocated_amount": {"type": "string"},
                    "explanation": {"type": "string"},
                },
            },
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "explanation": {"type": "string"},
    },
}


def _compact(value):
    # type: (Optional[str]) -> str
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


def _kind(document):
    # type: (Document) -> str
    return note_value(document.notes, "kind") or ""


def _category_code(session, document):
    # type: (Session, Document) -> Optional[str]
    if document.category_id is None:
        return None
    category = session.get(Category, document.category_id)
    if category is None:
        return None
    return category.code


def eligible(session, document):
    # type: (Session, Document) -> bool
    """Documents a bank line may use. personal_account alone does not exclude."""
    if document.status in ("error", "no_document", "duplicate", "pago", "recebido"):
        return False
    code = _category_code(session, document)
    if excluded_from_linking(code, document.notes, document.status):
        return False
    if (note_value(document.notes, "cash_movement") or "") == "no":
        return False
    if suspect_flags(risk_flag_names(document.notes)):
        return False
    return True


def cash_amount(document):
    # type: (Document) -> Optional[Decimal]
    """Amount a bank line should match. Withholding uses the net, not the gross."""
    for key in ("net_paid", "cash_amount"):
        raw = note_value(document.notes, key)
        if raw:
            return to_money(raw)
    if document.total is None or document.total < ZERO:
        return None
    return to_money(document.total)


def money_in(document):
    # type: (Document) -> bool
    """True when the company receives the money."""
    kind = _kind(document)
    if kind == "payslip":
        return False
    if kind == "payout_report":
        return True
    if kind in ("tax_guide", "payment_proof"):
        return False
    if (note_value(document.notes, "normal_reimbursement") or "") == "yes":
        return False
    if document.tipo == TIPO_RECEITA:
        return True
    if document.tipo in (TIPO_GASTO, TIPO_PESSOAL, TIPO_INVESTIMENTO, TIPO_ESTADO):
        return False
    if document.tipo == TIPO_SETTLEMENT and document.direction == "sale":
        return True
    return document.direction == "sale"


def bank_in(line):
    # type: (BankLine) -> bool
    return line.credit is not None and to_money(line.credit) > ZERO


def _document_dates(document):
    # type: (Document) -> List[date]
    found = []  # type: List[date]
    if document.document_date is not None:
        found.append(document.document_date)
    if document.payment_date is not None:
        found.append(document.payment_date)
    for key in ("due_date", "scheduled_payment"):
        raw = note_value(document.notes, key)
        if not raw:
            continue
        try:
            parsed = parse_iso_date(raw)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            found.append(parsed)
    return found


def within_days(line, document, days):
    # type: (BankLine, Document, int) -> bool
    for item in _document_dates(document):
        if abs((line.movement_date - item).days) <= days:
            return True
    return False


def name_tokens(entity):
    # type: (Optional[str]) -> List[str]
    words = re.findall(r"[A-Za-zÀ-ÿ]{4,}", entity or "")
    tokens = []  # type: List[str]
    for word in words:
        token = word.lower()
        if token in _NAME_STOP or token in tokens:
            continue
        tokens.append(token)
    return tokens


def name_overlap(entity, description):
    # type: (Optional[str], Optional[str]) -> List[str]
    blob = (description or "").lower()
    compact = "".join(ch for ch in blob if ch.isalnum())
    hits = []  # type: List[str]
    for token in name_tokens(entity):
        if token in blob or token in compact:
            hits.append(token)
    return hits


def reference_tokens(document):
    # type: (Document) -> List[str]
    """Own number, payment reference and booking codes. Not the invoice a receipt settles."""
    tokens = []  # type: List[str]

    def add(raw):
        # type: (str) -> None
        token = _compact(raw)
        if len(token) < 6 or token in tokens:
            return
        if token.isdigit() and len(token) < 8:
            return
        tokens.append(token)

    add(document.document_number or "")
    raw_tokens = note_value(document.notes, "match_tokens") or ""
    for piece in raw_tokens.split(","):
        add(piece)
    if document.lines_json:
        for match in _BOOKING_RE.finditer(document.lines_json.upper()):
            add(match.group(1))
    return tokens


def _parse_rate(value):
    # type: (str) -> Decimal
    text = value.strip().replace(" ", "")
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    return Decimal(text)


def fx_payment(description):
    # type: (str) -> Optional[Tuple[Decimal, str, Decimal, Decimal]]
    """Return (foreign amount, currency, rate, eur amount) when the line states a rate."""
    match = _FX_RE.search(description or "")
    if match is None:
        return None
    foreign = to_money(match.group(1).replace(".", "").replace(",", "."))
    currency = match.group(2).upper()
    if currency == "EUR":
        return None
    rate = _parse_rate(match.group(3))
    if rate <= ZERO:
        return None
    eur = to_money(foreign / rate)
    return foreign, currency, rate, eur


def line_components(document):
    # type: (Document) -> List[Decimal]
    """Positive line totals that add up to the document. Gross payout lines do not."""
    if not document.lines_json:
        return []
    try:
        rows = json.loads(document.lines_json)
    except (TypeError, ValueError):
        return []
    parts = []  # type: List[Decimal]
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            amount = to_money(abs(Decimal(str(row.get("total") or "0"))))
        except Exception:
            continue
        if amount > ZERO:
            parts.append(amount)
    if len(parts) < 2:
        return []
    total = to_money(sum(parts, ZERO))
    target = to_money(abs(document.total)) if document.total is not None else None
    if target is None or not money_equal(total, target):
        return []
    return parts


class AllocationBook(object):
    """How much of each line and document is already explained."""

    def __init__(self, existing):
        # type: (Sequence[Link]) -> None
        self.line_used = {}  # type: Dict[int, Decimal]
        self.doc_used = {}  # type: Dict[int, Decimal]
        for link in existing:
            self._add(link.bank_line_id, link.document_id, to_money(link.allocated_amount))

    def _add(self, line_id, document_id, amount):
        # type: (int, int, Decimal) -> None
        self.line_used[line_id] = to_money(self.line_used.get(line_id, ZERO) + amount)
        self.doc_used[document_id] = to_money(
            self.doc_used.get(document_id, ZERO) + amount
        )

    def line_open(self, line):
        # type: (BankLine) -> Decimal
        return to_money(abs(line.amount) - self.line_used.get(line.id or 0, ZERO))

    def line_done(self, line):
        # type: (BankLine) -> bool
        return money_equal(self.line_open(line), ZERO)

    def doc_open(self, document):
        # type: (Document) -> Optional[Decimal]
        cap = cash_amount(document)
        if cap is None or document.id is None:
            return None
        return to_money(cap - self.doc_used.get(document.id, ZERO))

    def can_take(self, line, document, amount):
        # type: (BankLine, Document, Decimal) -> bool
        amount = to_money(amount)
        if amount <= ZERO or line.id is None or document.id is None:
            return False
        if amount - self.line_open(line) > AMOUNT_TOLERANCE:
            return False
        room = self.doc_open(document)
        if room is None or amount - room > AMOUNT_TOLERANCE:
            return False
        return True

    def remember(self, line, document, amount):
        # type: (BankLine, Document, Decimal) -> None
        self._add(line.id or 0, document.id or 0, to_money(amount))


def _existing_pair(session, document_id, line_id):
    # type: (Session, int, int) -> Optional[Link]
    return session.exec(
        select(Link).where(
            Link.document_id == document_id,
            Link.bank_line_id == line_id,
        )
    ).first()


def _save_link(session, book, document, line, amount, method, explanation, confidence):
    # type: (Session, AllocationBook, Document, BankLine, Decimal, str, str, float) -> bool
    amount = to_money(amount)
    if document.id is None or line.id is None:
        return False
    if not book.can_take(line, document, amount):
        return False
    if _existing_pair(session, document.id, line.id) is not None:
        return False
    link = Link(
        document_id=document.id,
        bank_line_id=line.id,
        allocated_amount=amount,
        method=method,
        explanation=" ".join(explanation.split()),
        confidence=float(confidence),
    )
    session.add(link)
    book.remember(line, document, amount)
    line.status = "linked"
    session.add(line)
    return True


def _score_reference(line, document, token):
    # type: (BankLine, Document, str) -> int
    score = 10
    own = _compact(document.document_number or "")
    if token == own:
        score += 80
    description = (line.description or "").lower()
    if "payout" in description and _kind(document) == "payout_report":
        score += 40
    if money_in(document) == bank_in(line):
        score += 15
    amount = cash_amount(document)
    if amount is not None and money_equal(amount, abs(line.amount)):
        score += 25
    if within_days(line, document, DATE_WINDOW_DAYS):
        score += 5
    return score


def pass_reference(session, book, lines, documents):
    # type: (Session, AllocationBook, Sequence[BankLine], Sequence[Document]) -> int
    """Document number or reference printed in the bank description."""
    created = 0
    for line in lines:
        if book.line_done(line):
            continue
        description = _compact(line.description)
        if not description:
            continue
        ranked = []  # type: List[Tuple[int, Document, str]]
        for document in documents:
            if money_in(document) != bank_in(line):
                continue
            room = book.doc_open(document)
            if room is None or book.line_open(line) - room > AMOUNT_TOLERANCE:
                continue
            for token in reference_tokens(document):
                if token in description:
                    ranked.append((_score_reference(line, document, token), document, token))
                    break
        if not ranked:
            continue
        ranked.sort(key=lambda item: item[0], reverse=True)
        best_score, best, token = ranked[0]
        if len(ranked) > 1 and ranked[1][0] == best_score and ranked[1][1].id != best.id:
            continue
        amount = book.line_open(line)
        explanation = (
            "Bank description contains %s, the reference on %s %s."
            % (token, best.document_number, best.entity)
        )
        if _save_link(session, book, best, line, amount, "reference", explanation, 0.96):
            created += 1
    return created


def _exact_candidates(book, line, documents, fx):
    # type: (AllocationBook, BankLine, Sequence[Document], bool) -> List[Document]
    found = []  # type: List[Document]
    target = book.line_open(line)
    parsed = fx_payment(line.description) if fx else None
    for document in documents:
        if money_in(document) != bank_in(line):
            continue
        if not within_days(line, document, DATE_WINDOW_DAYS):
            continue
        if not name_overlap(document.entity, line.description):
            continue
        if fx:
            if parsed is None:
                continue
            foreign, currency, _rate, eur = parsed
            if not money_equal(eur, target):
                continue
            note_currency = (note_value(document.notes, "currency") or "").upper()
            if note_currency and note_currency != currency:
                continue
            amount = cash_amount(document)
            if amount is None or not money_equal(amount, foreign):
                continue
        else:
            amount = cash_amount(document)
            if amount is None or not money_equal(amount, target):
                continue
        room = book.doc_open(document)
        if room is None or target - room > AMOUNT_TOLERANCE:
            continue
        found.append(document)
    return found


def _pick_exact(line, candidates):
    # type: (BankLine, Sequence[Document]) -> Optional[Document]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    def sort_key(document):
        # type: (Document) -> Tuple[int, int]
        overlap = len(name_overlap(document.entity, line.description))
        distance = 999
        for item in _document_dates(document):
            distance = min(distance, abs((line.movement_date - item).days))
        kind_bias = 0
        if _kind(document) == "payment_proof":
            kind_bias = -1
        return (-overlap, distance, kind_bias, document.id or 0)

    ordered = sorted(candidates, key=sort_key)
    if sort_key(ordered[0]) == sort_key(ordered[1]):
        return None
    return ordered[0]


def pass_exact(session, book, lines, documents):
    # type: (Session, AllocationBook, Sequence[BankLine], Sequence[Document]) -> int
    """Same amount, compatible side, a date within 10 days, and a shared name token."""
    created = 0
    for line in lines:
        if book.line_done(line):
            continue
        chosen = _pick_exact(line, _exact_candidates(book, line, documents, False))
        if chosen is None:
            continue
        paid = cash_amount(chosen)
        net = note_value(chosen.notes, "net_paid")
        if net:
            explanation = (
                "%s matches the net %s paid on %s; gross %s stays on the document "
                "and the withholding remains a liability."
                % (line.description, paid, chosen.document_number, chosen.total)
            )
        else:
            explanation = (
                "%s matches %s %s (%s) on amount, side, date and name."
                % (to_money(abs(line.amount)), chosen.document_number, chosen.entity, paid)
            )
        if _save_link(
            session, book, chosen, line, book.line_open(line), "exact", explanation, 0.9
        ):
            created += 1
    for line in lines:
        if book.line_done(line):
            continue
        parsed = fx_payment(line.description)
        if parsed is None:
            continue
        chosen = _pick_exact(line, _exact_candidates(book, line, documents, True))
        if chosen is None:
            continue
        foreign, currency, rate, eur = parsed
        explanation = (
            "%s %s at %s equals the %s EUR charge for %s %s."
            % (foreign, currency, rate, eur, chosen.document_number, chosen.entity)
        )
        if _save_link(
            session, book, chosen, line, book.line_open(line), "exact", explanation, 0.88
        ):
            created += 1
    return created


def _party_key(document):
    # type: (Document) -> str
    nif = norm_nif(document.nif)
    if nif:
        return "nif:" + nif
    return "name:" + " ".join((document.entity or "").lower().split())


def _subsets_that_sum(docs, target):
    # type: (Sequence[Document], Decimal) -> List[List[Document]]
    """Subsets of 2 to 4 documents whose cash amounts sum to target."""
    usable = []  # type: List[Document]
    for document in docs:
        amount = cash_amount(document)
        if amount is None or amount <= ZERO or amount - target > AMOUNT_TOLERANCE:
            continue
        usable.append(document)
    found = []  # type: List[List[Document]]

    def walk(start, chosen, total):
        # type: (int, List[Document], Decimal) -> None
        if len(chosen) >= 2 and money_equal(total, target):
            found.append(list(chosen))
            return
        if len(chosen) >= 4 or len(found) > 4:
            return
        for index in range(start, len(usable)):
            amount = cash_amount(usable[index]) or ZERO
            if total + amount - target > AMOUNT_TOLERANCE:
                continue
            chosen.append(usable[index])
            walk(index + 1, chosen, to_money(total + amount))
            chosen.pop()

    walk(0, [], ZERO)
    return found


def pass_n_to_1(session, book, lines, documents):
    # type: (Session, AllocationBook, Sequence[BankLine], Sequence[Document]) -> int
    """One bank line that pays 2 to 4 documents of the same counterparty."""
    created = 0
    for line in lines:
        if book.line_done(line):
            continue
        if not name_overlap_any(line, documents):
            continue
        target = book.line_open(line)
        grouped = {}  # type: Dict[str, List[Document]]
        for document in documents:
            if money_in(document) != bank_in(line):
                continue
            if not within_days(line, document, 21):
                continue
            room = book.doc_open(document)
            amount = cash_amount(document)
            if room is None or amount is None or not money_equal(room, amount):
                continue
            if not name_overlap(document.entity, line.description):
                continue
            grouped.setdefault(_party_key(document), []).append(document)
        winner = None  # type: Optional[List[Document]]
        ambiguous = False
        for group in grouped.values():
            subsets = _subsets_that_sum(group, target)
            if len(subsets) == 1 and winner is None:
                winner = subsets[0]
            elif subsets:
                ambiguous = True
                break
        if ambiguous or not winner:
            continue
        numbers = ", ".join(
            "%s %s" % (item.document_number, cash_amount(item)) for item in winner
        )
        for document in winner:
            amount = cash_amount(document) or ZERO
            explanation = (
                "Bank line %s is the sum of %s from %s."
                % (to_money(target), numbers, document.entity)
            )
            if _save_link(session, book, document, line, amount, "n_to_1", explanation, 0.86):
                created += 1
    return created


def name_overlap_any(line, documents):
    # type: (BankLine, Sequence[Document]) -> bool
    for document in documents:
        if name_overlap(document.entity, line.description):
            return True
    return False


def pass_one_to_n(session, book, lines, documents):
    # type: (Session, AllocationBook, Sequence[BankLine], Sequence[Document]) -> int
    """One document explained by several bank lines, such as a fee note or an FX cluster."""
    created = 0
    open_lines = [line for line in lines if not book.line_done(line)]
    for document in documents:
        parts = line_components(document)
        if len(parts) < 2:
            continue
        if money_in(document):
            continue
        used = []  # type: List[Tuple[BankLine, Decimal]]
        pool = list(open_lines)
        matched = True
        for part in parts:
            choice = None  # type: Optional[BankLine]
            best_distance = 999
            for line in pool:
                if bank_in(line):
                    continue
                if book.line_done(line) or any(line.id == taken.id for taken, _part in used):
                    continue
                if not money_equal(abs(line.amount), part):
                    continue
                distance = abs((line.movement_date - document.document_date).days)
                if choice is None or distance < best_distance:
                    choice = line
                    best_distance = distance
            if choice is None:
                matched = False
                break
            used.append((choice, part))
        if not matched or len(used) < 2:
            continue
        detail = ", ".join(str(to_money(part)) for _line, part in used)
        for line, part in used:
            explanation = (
                "%s is one of the charges on %s %s (%s), which sum to %s."
                % (
                    to_money(part),
                    document.document_number,
                    document.entity,
                    detail,
                    document.total,
                )
            )
            if _save_link(session, book, document, line, part, "1_to_n", explanation, 0.9):
                created += 1
        open_lines = [line for line in lines if not book.line_done(line)]
    created += _fx_clusters(session, book, lines, documents)
    return created


def _looks_like_fee(description):
    # type: (str) -> bool
    blob = (description or "").lower()
    markers = ("comiss", "imposto selo", "moeda estrangeira", "stamp")
    return any(marker in blob for marker in markers)


def _fx_clusters(session, book, lines, documents):
    # type: (Session, AllocationBook, Sequence[BankLine], Sequence[Document]) -> int
    """Foreign-currency purchase plus the bank fee and stamp duty on the same day.

    Fee lines already claimed by a debit note that itemises them are left alone.
    """
    created = 0
    ordered = list(lines)
    for index, line in enumerate(ordered):
        if book.line_done(line):
            continue
        parsed = fx_payment(line.description)
        if parsed is None:
            continue
        foreign, currency, rate, eur = parsed
        if not money_equal(abs(line.amount), eur):
            continue
        chosen = None  # type: Optional[Document]
        for document in documents:
            if money_in(document) or book.line_done(line):
                continue
            amount = cash_amount(document)
            if amount is None or not money_equal(amount, foreign):
                continue
            note_currency = (note_value(document.notes, "currency") or "").upper()
            if note_currency and note_currency != currency:
                continue
            if not name_overlap(document.entity, line.description):
                continue
            if not within_days(line, document, DATE_WINDOW_DAYS):
                continue
            chosen = document
            break
        if chosen is None:
            continue
        cluster = [(line, book.line_open(line))]
        for follower in ordered[index + 1 :]:
            if follower.movement_date != line.movement_date:
                break
            if book.line_done(follower) or not _looks_like_fee(follower.description):
                break
            cluster.append((follower, book.line_open(follower)))
        if len(cluster) < 2:
            continue
        for item, amount in cluster:
            explanation = (
                "%s is part of the %s %s payment for %s at rate %s, with the bank charges."
                % (to_money(amount), foreign, currency, chosen.document_number, rate)
            )
            if _save_link(session, book, chosen, item, amount, "1_to_n", explanation, 0.84):
                created += 1
    return created


def _plausible(line, document):
    # type: (BankLine, Document) -> bool
    if document.period != line.period and not within_days(line, document, 45):
        return False
    amount = cash_amount(document)
    if amount is None:
        return False
    bank = to_money(abs(line.amount))
    if money_equal(amount, bank):
        return True
    if name_overlap(document.entity, line.description) and (
        amount >= bank or bank - amount <= to_money(bank * Decimal("0.2"))
    ):
        return True
    return False


def _candidate_rows(book, line, documents):
    # type: (AllocationBook, BankLine, Sequence[Document]) -> List[Document]
    rows = []  # type: List[Document]
    for document in documents:
        if money_in(document) != bank_in(line):
            continue
        room = book.doc_open(document)
        if room is None or room <= ZERO:
            continue
        if not _plausible(line, document):
            continue
        rows.append(document)
    rows.sort(
        key=lambda document: (
            abs((cash_amount(document) or ZERO) - abs(line.amount)),
            0 if name_overlap(document.entity, line.description) else 1,
            document.id or 0,
        )
    )
    chosen = rows[:8]
    ids = set(document.id for document in chosen)
    for document in list(chosen):
        ref = _compact(document.referenced_invoice or "")
        if not ref:
            continue
        for other in documents:
            if other.id in ids:
                continue
            if _compact(other.document_number or "") == ref and book.doc_open(other):
                chosen.append(other)
                ids.add(other.id)
    return chosen[:10]


def _proposal_ok(book, line, candidates, allocations):
    # type: (AllocationBook, BankLine, Sequence[Document], List[Dict[str, Any]]) -> Optional[str]
    if not allocations:
        return None
    by_id = {}
    for document in candidates:
        by_id[document.id] = document
    total = ZERO
    seen = set()
    for item in allocations:
        document_id = item["document_id"]
        if document_id in seen or document_id not in by_id:
            return None
        seen.add(document_id)
        amount = to_money(item["allocated_amount"])
        if amount <= ZERO:
            return None
        if not book.can_take(line, by_id[document_id], amount):
            return None
        total += amount
    if not money_equal(total, abs(line.amount)):
        return "sum"
    return "ok"


def pass_llm(session, book, lines, documents):
    # type: (Session, AllocationBook, Sequence[BankLine], Sequence[Document]) -> int
    """Ask for an allocation. Discard it unless the amounts sum to the bank line."""
    created = 0
    for line in lines:
        if book.line_done(line):
            continue
        candidates = _candidate_rows(book, line, documents)
        if not candidates:
            continue
        payload = []
        for document in candidates:
            payload.append(
                {
                    "document_id": document.id,
                    "date": document.document_date.isoformat(),
                    "entity": document.entity,
                    "number": document.document_number,
                    "total": str(document.total),
                    "cash_amount": str(cash_amount(document)),
                    "kind": _kind(document),
                    "tipo": document.tipo,
                    "referenced_invoice": document.referenced_invoice,
                    "net_paid": note_value(document.notes, "net_paid"),
                }
            )
        prompt = "\n".join(
            [
                "Allocate this unmatched bank line to one or more of the candidate documents.",
                "Reply with JSON. allocations is a list of {document_id, allocated_amount, explanation}.",
                "allocated_amount is a string with two decimals. The amounts must sum to the bank line within 0.01.",
                "Use an empty allocations list when none of the candidates support the line.",
                "A payment can differ from a document because of tax withholding (allocate the net), a platform commission, an FX conversion, a partial payment, or a settlement receipt plus the invoice it settles.",
                "Do not allocate the same euro twice. If a receipt settles an invoice, allocate the bank amount once, to the receipt when the receipt equals the amount paid.",
                "Initials in the description can be the counterparty (AMV is Ateliê Moldura Viva).",
                "confidence is 0-1. explanation is one line.",
                "",
                "Bank line %s on %s: %s amount %s (%s)"
                % (
                    line.id,
                    line.movement_date.isoformat(),
                    line.description,
                    to_money(abs(line.amount)),
                    "credit" if bank_in(line) else "debit",
                ),
                "",
                "Candidates:",
                json.dumps(payload, ensure_ascii=False),
            ]
        )
        try:
            data = llm_json(prompt, LINK_SCHEMA)
        except (LLMError, Exception):
            logger.exception("link proposal failed for bank line %s", line.id)
            continue
        allocations = data.get("allocations") or []
        verdict = _proposal_ok(book, line, candidates, allocations)
        if verdict != "ok":
            logger.info(
                "discarded link proposal for bank line %s (%s)",
                line.id,
                verdict or "empty",
            )
            continue
        confidence = float(data.get("confidence") or 0)
        by_id = {}
        for document in candidates:
            by_id[document.id] = document
        for item in allocations:
            document = by_id[int(item["document_id"])]
            explanation = item.get("explanation") or data.get("explanation") or "LLM allocation"
            amount = to_money(item["allocated_amount"])
            if _save_link(session, book, document, line, amount, "llm", explanation, confidence):
                created += 1
    return created


def _party_from_description(description):
    # type: (str) -> str
    text = description or ""
    text = re.sub(
        r"\b(TRF|CRED|SEPA\+?|IMED|COMPRA|PAG|ESTADO|SERV|MBWAY|CAMBIO|USD|EUR)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\b\d{4,}\b", " ", text)
    text = re.sub(r"\d+,\d{2}", " ", text)
    text = text.replace("*", " ").replace("+", " ")
    return " ".join(text.split())


def _missing_item(session, line):
    # type: (Session, BankLine) -> None
    marker = "bank_line_id: %s" % line.id
    rows = session.exec(
        select(OpenItem).where(OpenItem.kind == "missing_document")
    ).all()
    existing = None
    for item in rows:
        if item.notes and marker in item.notes and "linker:" in (item.notes or ""):
            existing = item
            break
    party = _party_from_description(line.description) or line.description
    side = "incoming" if bank_in(line) else "outgoing"
    description = (
        "Owner: bank line %s on %s (%s %s) has no supporting document. "
        "Probably missing a document for %s."
        % (line.id, line.movement_date.isoformat(), to_money(abs(line.amount)), side, party)
    )
    notes = " | ".join(
        [
            "linker: missing_document",
            marker,
            "description: %s" % line.description,
        ]
    )
    if existing is None:
        if session.exec(select(Period).where(Period.month == line.period)).first() is None:
            session.add(Period(month=line.period, status="open"))
            session.commit()
        item = OpenItem(
            document_id=None,
            period=line.period,
            kind="missing_document",
            description=description,
            amount=to_money(abs(line.amount)),
            due_date=None,
            status="open",
            notes=notes,
            assignee="owner",
            confidence=0.6,
            justification="No eligible document matched this bank line.",
        )
        session.add(item)
        return
    existing.description = description
    existing.amount = to_money(abs(line.amount))
    existing.status = "open"
    existing.notes = notes
    existing.assignee = "owner"
    existing.period = line.period
    session.add(existing)


def _close_missing(session, line):
    # type: (Session, BankLine) -> None
    marker = "bank_line_id: %s" % line.id
    rows = session.exec(
        select(OpenItem).where(OpenItem.kind == "missing_document")
    ).all()
    for item in rows:
        if item.notes and marker in item.notes and item.status == "open":
            item.status = "done"
            session.add(item)


def mark_unsupported(session, book, lines):
    # type: (Session, AllocationBook, Sequence[BankLine]) -> int
    """Lines with nothing linked stay unsupported and become an owner task."""
    count = 0
    for line in lines:
        if book.line_done(line):
            line.status = "linked"
            session.add(line)
            _close_missing(session, line)
            continue
        line.status = "unsupported"
        session.add(line)
        _missing_item(session, line)
        count += 1
    return count


def clear_links(session):
    # type: (Session) -> None
    """Drop links and the missing-document tasks this linker created."""
    for link in session.exec(select(Link)).all():
        session.delete(link)
    for line in session.exec(select(BankLine)).all():
        line.status = "unsupported"
        session.add(line)
    for item in session.exec(select(OpenItem)).all():
        notes = item.notes or ""
        if item.kind in ("missing_document", "missing_sales_invoice") and "linker:" in notes:
            session.delete(item)
    session.commit()


def link_all(session, relink=False):
    # type: (Session, bool) -> Dict[str, Any]
    """Idempotent. --relink drops previous links and computes them again."""
    if relink:
        clear_links(session)
    documents = [
        document
        for document in session.exec(select(Document)).all()
        if eligible(session, document)
    ]
    lines = session.exec(
        select(BankLine).order_by(BankLine.movement_date, BankLine.id)
    ).all()
    existing = session.exec(select(Link)).all()
    book = AllocationBook(existing)
    counts = {
        "reference": pass_reference(session, book, lines, documents),
        "exact": pass_exact(session, book, lines, documents),
        "n_to_1": pass_n_to_1(session, book, lines, documents),
        "1_to_n": pass_one_to_n(session, book, lines, documents),
    }
    session.commit()
    documents = [
        document
        for document in session.exec(select(Document)).all()
        if eligible(session, document)
    ]
    lines = session.exec(
        select(BankLine).order_by(BankLine.movement_date, BankLine.id)
    ).all()
    existing = session.exec(select(Link)).all()
    book = AllocationBook(existing)
    counts["llm"] = pass_llm(session, book, lines, documents)
    session.commit()
    from app.complete import complete_link_sets

    for key, value in complete_link_sets(session).items():
        counts[key] = value
    session.commit()
    lines = session.exec(
        select(BankLine).order_by(BankLine.movement_date, BankLine.id)
    ).all()
    existing = session.exec(select(Link)).all()
    book = AllocationBook(existing)
    counts["unsupported"] = mark_unsupported(session, book, lines)
    session.commit()
    return counts

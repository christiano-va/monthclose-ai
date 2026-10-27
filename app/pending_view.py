"""Pending list for the owner. Reads items; it does not decide them."""
from typing import Any, Dict, List

from sqlmodel import Session, select

from app.archive import first_statement_month, is_archived_period
from app.i18n import text
from app.models import Category, Document, OpenItem
from app.money import money_or_unknown
from app.pending import resolved_after_close
from app.posting import note_value
from app.present import (
    category_label,
    is_suspect_document,
    item_heading,
    plain_reason,
    replace_codes,
)
from app.ui import (
    chip,
    esc,
    month_name_year,
    render_page,
    show_amount,
    show_date,
    weekday_date,
)

_NEXT = {
    "missing_document": "next_find_document",
    "missing_sales_invoice": "next_issue_invoice",
    "invoice_to_request": "next_ask_invoice",
    "awaiting_bank_line": "next_bank",
    "confirm_late_document": "next_confirm",
    "confirm_explanation": "next_confirm",
    "payable": "next_pay",
    "receivable": "next_collect",
}


def _reason(lang, item, summary=""):
    # type: (str, OpenItem, str) -> str
    if item.status == "accepted_by_owner" and (item.approval_reason or "").strip():
        return plain_reason(lang, item.approval_reason)
    if (summary or "").strip():
        return plain_reason(lang, summary)
    justification = (item.justification or "").strip()
    if justification:
        return plain_reason(lang, justification)
    if (item.approval_reason or "").strip():
        return plain_reason(lang, item.approval_reason)
    key = "reason_%s" % (item.kind or "")
    label = text(lang, key)
    if label != key:
        return label
    return text(lang, "reason_default")


def _next_action(lang, status, kind, notes):
    # type: (str, str, str, str) -> str
    if notes and "credit_balance:" in notes:
        return text(lang, "next_credit")
    if status == "accepted_by_owner":
        if kind == "missing_sales_invoice":
            return text(lang, "next_accepted_invoice")
        return text(lang, "next_accepted")
    if status and ("resolved" in status or status == "done"):
        return text(lang, "next_resolved_quiet")
    return text(lang, _NEXT.get(kind, "next_review"))


def _card(lang, what, who, amount, kind, status, reason, action, due=""):
    # type: (str, str, str, object, str, str, str, str, str) -> str
    who_html = ""
    if who and who != what:
        who_html = "<p class='meta-line'>%s</p>" % esc(who)
    due_html = ""
    if due:
        due_html = "<p class='meta-line'>%s</p>" % esc(weekday_date(lang, due) or show_date(lang, due))
    return """
<article class="card item-card">
  %s
  <h2>%s</h2>
  <p class="total">%s</p>
  %s
  <p>%s</p>
  <p class="reason"><strong>%s</strong> %s</p>
  <p class="next-action"><strong>%s</strong> %s</p>
</article>
""" % (
        who_html,
        esc(what),
        esc(show_amount(lang, amount)),
        due_html,
        chip(lang, status),
        esc(text(lang, "pending_reason")),
        esc(reason),
        esc(text(lang, "pending_next")),
        esc(action),
    )


def _item_card(lang, item, entity, summary=""):
    # type: (str, OpenItem, str, str) -> str
    heading = item_heading(lang, item.kind, item.notes, item.description, entity)
    status = item.status or "open"
    due = item.due_date.isoformat() if item.due_date else ""
    amount = money_or_unknown(item.amount)
    if item.notes and "credit_balance:" in item.notes and str(amount).startswith("-"):
        amount = str(amount)[1:]
    return _card(
        lang,
        heading["what"],
        heading["who"],
        amount,
        item.kind,
        status,
        _reason(lang, item, summary),
        _next_action(lang, status, item.kind, item.notes or ""),
        due,
    )


def _month_blocks(lang, grouped, order):
    # type: (str, Dict[str, List[str]], List[str]) -> str
    blocks = []
    for month in order:
        cards = grouped.get(month) or []
        if not cards:
            continue
        blocks.append(
            "<section class='month-group'><h2>%s</h2>%s</section>"
            % (esc(month_name_year(lang, month)), "".join(cards))
        )
    return "".join(blocks)


def _documents(session):
    # type: (Session) -> Dict[int, Document]
    found = {}  # type: Dict[int, Document]
    for document in session.exec(select(Document)).all():
        if document.id is not None:
            found[document.id] = document
    return found


def _categories(session):
    # type: (Session) -> Dict[int, Category]
    found = {}  # type: Dict[int, Category]
    for category in session.exec(select(Category)).all():
        if category.id is not None:
            found[category.id] = category
    return found


def _document_card(lang, document, category):
    # type: (str, Document, object) -> str
    code = category.code if category is not None else ""
    description = category.description if category is not None else ""
    label = category_label(lang, code, description or "")
    reason = plain_reason(lang, note_value(document.notes, "reasons") or document.justification or "")
    if not reason:
        reason = plain_reason(lang, document.justification or "")
    reason = replace_codes(lang, reason)
    return """
<article class="card item-card">
  <p class="meta-line">%s</p>
  <h2>%s</h2>
  <p class="total">%s</p>
  <p class="meta-line">%s</p>
  <p>%s</p>
  <p class="reason"><strong>%s</strong> %s</p>
</article>
""" % (
        esc(label),
        esc(document.entity or "—"),
        esc(show_amount(lang, document.total)),
        esc(show_date(lang, document.document_date)),
        chip(lang, "suspect" if code == "SUSPECT" or document.tipo == "suspeito" else document.status),
        esc(text(lang, "pending_reason")),
        esc(reason),
    )


def _filtered_documents(session, lang, focus):
    # type: (Session, str, str) -> str
    documents = session.exec(select(Document).order_by(Document.period.desc(), Document.id.desc())).all()
    categories = _categories(session)
    cutoff = first_statement_month(session)
    grouped = {}  # type: Dict[str, List[str]]
    order = []  # type: List[str]
    for document in documents:
        if is_archived_period(document.period, cutoff):
            continue
        category = categories.get(document.category_id) if document.category_id else None
        code = category.code if category is not None else ""
        if focus == "suspect":
            if not is_suspect_document(document, code):
                continue
        elif document.status != "review":
            continue
        month = document.period or ""
        if month not in grouped:
            order.append(month)
            grouped[month] = []
        grouped[month].append(_document_card(lang, document, category))
    if focus == "suspect":
        title = text(lang, "pending_suspect_title")
        empty = text(lang, "pending_suspect_empty")
    else:
        title = text(lang, "pending_review_title")
        empty = text(lang, "pending_review_empty")
    body_cards = _month_blocks(lang, grouped, order)
    if not body_cards:
        body_cards = "<p class='quiet'>%s</p>" % esc(empty)
    return "<h1>%s</h1><p class='lead'>%s</p>%s" % (
        esc(title),
        esc(text(lang, "pending_intro")),
        body_cards,
    )


def render_pending(session, month, months, lang, focus=""):
    # type: (Session, str, List[str], str, str) -> str
    path = "/pending"
    if focus in ("review", "suspect"):
        body = _filtered_documents(session, lang, focus)
        path = "/pending?focus=%s" % focus
        title = text(lang, "pending_review_title" if focus == "review" else "pending_suspect_title")
        return render_page(lang, "pending", month, months, title, body, path)

    documents = _documents(session)
    cutoff = first_statement_month(session)
    items = session.exec(select(OpenItem).order_by(OpenItem.period.desc(), OpenItem.id)).all()
    periods = []  # type: List[str]
    for item in items:
        if is_archived_period(item.period, cutoff):
            continue
        if item.period and item.period not in periods:
            periods.append(item.period)
    resolved_ids = set()
    grouped = {}  # type: Dict[str, List[str]]
    for name in periods:
        if is_archived_period(name, cutoff):
            continue
        grouped[name] = []
        for row in resolved_after_close(session, name):
            if row.get("open_item_id"):
                resolved_ids.add(row["open_item_id"])
            document = documents.get(row.get("document_id"))
            entity = document.entity if document is not None else ""
            heading = item_heading(lang, row.get("kind") or "", "", row.get("item") or "", entity)
            card = _card(
                lang,
                heading["what"],
                entity,
                row.get("amount"),
                row.get("kind") or "",
                "resolved_by_late_document",
                plain_reason(lang, row.get("summary") or "") or text(lang, "reason_default"),
                text(lang, "next_resolved_quiet"),
            )
            grouped[name].append(card)

    buckets = {"open": [], "accepted": [], "resolved": []}
    # Regroup cards by status inside each month, open items first.
    by_month = {}  # type: Dict[str, Dict[str, List[str]]]
    for name in periods:
        by_month[name] = {"open": [], "accepted": [], "resolved": list(grouped.get(name) or [])}

    for item in items:
        if is_archived_period(item.period, cutoff):
            continue
        if item.id in resolved_ids:
            continue
        status = item.status or "open"
        document = documents.get(item.document_id) if item.document_id else None
        entity = document.entity if document is not None else ""
        card = _item_card(lang, item, entity)
        month_name = item.period or ""
        if month_name not in by_month:
            periods.append(month_name)
            by_month[month_name] = {"open": [], "accepted": [], "resolved": []}
        if status == "open":
            by_month[month_name]["open"].append(card)
        elif status == "accepted_by_owner":
            by_month[month_name]["accepted"].append(card)
        elif "resolved" in status or status == "done":
            by_month[month_name]["resolved"].append(card)

    periods = sorted(set(periods), reverse=True)
    blocks = []
    for name in periods:
        parts = by_month.get(name) or buckets
        cards = parts["open"] + parts["accepted"] + parts["resolved"]
        if not cards:
            continue
        blocks.append(
            "<section class='month-group'><h2>%s</h2>%s</section>"
            % (esc(month_name_year(lang, name)), "".join(cards))
        )
    if blocks:
        inner = "".join(blocks)
    else:
        inner = "<p class='quiet'>%s</p>" % esc(text(lang, "pending_empty_open"))
    body = "<h1>%s</h1><p class='lead'>%s</p>%s" % (
        esc(text(lang, "pending_title")),
        esc(text(lang, "pending_intro")),
        inner,
    )
    title = text(lang, "pending_title")
    return render_page(lang, "pending", month, months, title, body, "/pending")

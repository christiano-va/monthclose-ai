"""Pending list for the owner. Reads items; it does not decide them."""
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from app.i18n import text
from app.models import OpenItem
from app.money import money_or_unknown
from app.pending import resolved_after_close
from app.ui import chip, esc, kind_label, month_title, render_page, show_amount, show_date

_GENERIC = "open item from history or inbox"

_NEXT = {
    "missing_document": "next_find_document",
    "missing_sales_invoice": "next_issue_invoice",
    "invoice_to_request": "next_ask_invoice",
    "awaiting_bank_line": "next_bank",
    "confirm_late_document": "next_confirm",
    "payable": "next_pay",
    "receivable": "next_collect",
}


def _reason(lang, item, summary=""):
    # type: (str, OpenItem, str) -> str
    if item.status == "accepted_by_owner" and (item.approval_reason or "").strip():
        return item.approval_reason.strip()
    if (summary or "").strip():
        return summary.strip()
    justification = (item.justification or "").strip()
    if justification and justification != _GENERIC:
        return justification
    if (item.approval_reason or "").strip():
        return item.approval_reason.strip()
    key = "reason_%s" % (item.kind or "")
    label = text(lang, key)
    if label != key:
        return label
    return text(lang, "reason_default")


def _next_action(lang, status, kind):
    # type: (str, str, str) -> str
    if status == "accepted_by_owner":
        if kind == "missing_sales_invoice":
            return text(lang, "next_accepted_invoice")
        return text(lang, "next_accepted")
    if status and ("resolved" in status or status == "done"):
        return text(lang, "next_resolved_quiet")
    return text(lang, _NEXT.get(kind, "next_review"))


def _card(lang, title, amount, kind, status, reason, action, due=""):
    # type: (str, str, object, str, str, str, str, str) -> str
    due_html = ""
    if due:
        due_html = "<p class='meta-line'>%s</p>" % esc(text(lang, "pending_due", date=show_date(lang, due)))
    return """
<article class="card item-card">
  <p class="meta-line">%s</p>
  <h2>%s</h2>
  <p class="total">%s</p>
  %s
  <p>%s</p>
  <p class="reason"><strong>%s</strong> %s</p>
  <p class="next-action"><strong>%s</strong> %s</p>
</article>
""" % (
        esc(kind_label(lang, kind)),
        esc(title),
        esc(show_amount(lang, amount)),
        due_html,
        chip(lang, status),
        esc(text(lang, "pending_reason")),
        esc(reason),
        esc(text(lang, "pending_next")),
        esc(action),
    )


def _section(heading, empty, cards):
    # type: (str, str, List[str]) -> str
    if cards:
        body = "".join(cards)
    else:
        body = "<p class='quiet'>%s</p>" % esc(empty)
    return "<section><h2>%s</h2>%s</section>" % (esc(heading), body)


def render_pending(session, month, months, lang):
    # type: (Session, str, List[str], str) -> str
    items = session.exec(
        select(OpenItem).where(OpenItem.period == month).order_by(OpenItem.id)
    ).all()
    resolutions = resolved_after_close(session, month)
    resolved_ids = set()
    open_cards = []  # type: List[str]
    accepted_cards = []  # type: List[str]
    resolved_cards = []  # type: List[str]

    for row in resolutions:
        if row.get("open_item_id"):
            resolved_ids.add(row["open_item_id"])
        resolved_cards.append(
            _card(
                lang,
                row.get("item") or "",
                row.get("amount"),
                row.get("kind") or "",
                "resolved_by_late_document",
                row.get("summary") or text(lang, "reason_default"),
                text(lang, "next_resolved_quiet"),
            )
        )

    for item in items:
        if item.id in resolved_ids:
            continue
        status = item.status or "open"
        due = item.due_date.isoformat() if item.due_date else ""
        card = _card(
            lang,
            item.description,
            money_or_unknown(item.amount),
            item.kind,
            status,
            _reason(lang, item),
            _next_action(lang, status, item.kind),
            due,
        )
        if status == "open":
            open_cards.append(card)
        elif status == "accepted_by_owner":
            accepted_cards.append(card)
        elif "resolved" in status or status == "done":
            resolved_cards.append(card)

    body = "<h1>%s</h1><p class='lead'>%s</p>%s%s%s" % (
        esc(text(lang, "pending_title")),
        esc(text(lang, "pending_intro")),
        _section(text(lang, "pending_open"), text(lang, "pending_empty_open"), open_cards),
        _section(text(lang, "pending_accepted"), text(lang, "pending_empty_accepted"), accepted_cards),
        _section(text(lang, "pending_resolved"), text(lang, "pending_empty_resolved"), resolved_cards),
    )
    title = "%s — %s" % (text(lang, "pending_title"), month_title(lang, month))
    return render_page(lang, "pending", month, months, title, body, "/pending")

"""Shared page chrome: header, language, month, and the tab bar."""
import html
import re
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Tuple

from sqlmodel import Session, select

from app.i18n import text
from app.models import BankLine, Period
from app.settings import company_name

_TEMPLATE = (Path(__file__).resolve().parent / "templates" / "base.html").read_text(
    encoding="utf-8"
)
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

_DOC_KEYS = {
    "fatura": "doc_fatura",
    "fatura-recibo": "doc_fatura_recibo",
    "fatura_recibo": "doc_fatura_recibo",
    "recibo": "doc_recibo",
    "talao": "doc_recibo",
    "talão": "doc_recibo",
    "nota_credito": "doc_nota_credito",
    "nota de credito": "doc_nota_credito",
    "guia": "doc_guia",
    "comprovativo": "doc_comprovativo",
    "recibo_vencimento": "doc_recibo_vencimento",
    "extrato": "doc_extrato",
    "invoice": "doc_invoice",
    "receipt": "doc_receipt",
    "payslip": "doc_payslip",
    "other": "doc_other",
}

_NAV = (
    ("dashboard", "/", "nav_dashboard"),
    ("scan", "/scan", "nav_scan"),
    ("report", "/report", "nav_report"),
    ("pending", "/pending", "nav_pending"),
)


def esc(value):
    # type: (object) -> str
    return html.escape("" if value is None else str(value), quote=True)


def resolve_lang(request):
    # type: (object) -> str
    """Portuguese unless the query or the lang cookie says English."""
    raw = (request.query_params.get("lang") or "").strip().lower()
    if raw in ("pt", "pt-pt", "en"):
        return "pt" if raw.startswith("pt") else "en"
    cookie = (request.cookies.get("lang") or "").strip().lower()
    if cookie in ("pt", "en"):
        return cookie
    return "pt"


def prefers_html(request):
    # type: (object) -> bool
    accept = request.headers.get("accept") or ""
    if "text/html" not in accept:
        return False
    html_at = accept.find("text/html")
    json_at = accept.find("application/json")
    if json_at == -1:
        return True
    return html_at < json_at


def selected_month(session, requested):
    # type: (Session, Optional[str]) -> Tuple[str, List[str]]
    periods = session.exec(select(Period).order_by(Period.month.desc())).all()
    months = [row.month for row in periods]  # type: List[str]
    chosen = requested if requested and _MONTH_RE.match(requested) else None
    if chosen is None:
        line = session.exec(
            select(BankLine).order_by(BankLine.period.desc(), BankLine.id.desc())
        ).first()
        if line is not None and line.period:
            chosen = line.period
        elif months:
            chosen = months[0]
        else:
            chosen = date.today().strftime("%Y-%m")
    if chosen not in months:
        months = [chosen] + months
    return chosen, months


def page_href(path, month, lang):
    # type: (str, str, str) -> str
    if path.startswith("/report/"):
        return "%s?lang=%s" % (path, lang)
    return "%s?month=%s&lang=%s" % (path, month, lang)


def show_date(lang, iso):
    # type: (str, object) -> str
    raw = "" if iso is None else str(iso)
    parts = raw.split("-")
    if len(parts) < 3 or len(parts[0]) != 4:
        return raw
    year, month_text, day = parts[0], parts[1], parts[2][:2]
    try:
        month_number = int(month_text)
        day_number = int(day)
    except ValueError:
        return raw
    if lang == "pt":
        return "%02d/%02d/%s" % (day_number, month_number, year)
    name = text(lang, "month_%d" % month_number) if 1 <= month_number <= 12 else month_text
    return "%d %s %s" % (day_number, name, year)


def long_date(lang, value):
    # type: (str, date) -> str
    name = text(lang, "month_%d" % value.month)
    if lang == "pt":
        return "%d de %s de %d" % (value.day, name, value.year)
    return "%d %s %d" % (value.day, name, value.year)


def month_title(lang, month):
    # type: (str, str) -> str
    year_text, _, month_text = (month or "").partition("-")
    try:
        index = int(month_text)
    except ValueError:
        return month
    if index < 1 or index > 12:
        return month
    name = text(lang, "month_%d" % index)
    if lang == "pt":
        return "%s de %s" % (name, year_text)
    return "%s %s" % (name, year_text)


def show_amount(lang, value):
    # type: (str, object) -> str
    raw = "" if value is None else str(value).strip()
    if raw in ("", "amount to be confirmed"):
        return text(lang, "amount_unknown")
    return "%s €" % raw


def document_type_label(lang, raw):
    # type: (str, object) -> str
    token = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    key = _DOC_KEYS.get(token) or _DOC_KEYS.get(str(raw or "").strip().lower())
    if key:
        return text(lang, key)
    if raw:
        return str(raw)
    return text(lang, "doc_other")


def chip_parts(status, category=""):
    # type: (object, object) -> Tuple[str, str]
    """Return the colour token and the dictionary key. The label is never colour alone."""
    text_status = str(status or "").strip().lower()
    code = str(category or "").strip().upper()
    if code == "SUSPECT" or "suspect" in text_status:
        return "suspect", "status_suspect"
    if text_status == "accepted_by_owner":
        return "resolved", "status_accepted"
    if text_status == "duplicate":
        return "resolved", "status_duplicate"
    if "resolved" in text_status or text_status == "done":
        return "resolved", "status_resolved"
    if text_status in ("review", "error"):
        return "review", "status_review"
    if text_status in ("validated", "pago", "recebido"):
        return "validated", "status_validated"
    return "pending", "status_pending"


def chip(lang, status, category=""):
    # type: (str, object, object) -> str
    tone, key = chip_parts(status, category)
    return "<span class='chip chip-%s'>%s</span>" % (tone, esc(text(lang, key)))


def month_status_label(lang, status):
    # type: (str, object) -> str
    if status == "closed":
        return text(lang, "status_closed_month")
    if status == "open":
        return text(lang, "status_open_month")
    return str(status or "")


def kind_label(lang, kind):
    # type: (str, object) -> str
    key = "kind_%s" % (kind or "")
    label = text(lang, key)
    if label == key:
        return str(kind or "")
    return label


def scan_next_action(card, lang):
    # type: (dict, str) -> str
    pending = card.get("pending_item") or None
    status = str(card.get("status") or "").lower()
    category = str(card.get("category") or "")
    entity = str(card.get("entity") or "").strip()
    if pending:
        name = entity or str(pending.get("description") or "").strip()
        if not name:
            name = kind_label(lang, pending.get("kind"))
        return text(lang, "next_resolved_item", entity=name)
    if "awaiting" in status:
        return text(lang, "next_bank")
    if status == "duplicate":
        return text(lang, "next_duplicate")
    if status in ("review", "error") or category.upper() == "SUSPECT":
        return text(lang, "next_review")
    if status in ("validated", "pago", "recebido"):
        return text(lang, "next_filed")
    return text(lang, "next_review")


def present_scan_card(card, lang):
    # type: (dict, str) -> dict
    tone, label_key = chip_parts(card.get("status"), card.get("category"))
    total = card.get("total")
    shown_total = show_amount(lang, total) if total not in (None, "") else "—"
    return {
        "document_type": document_type_label(lang, card.get("document_type")),
        "entity": card.get("entity") or "—",
        "total": shown_total,
        "date": show_date(lang, card.get("date")) or "—",
        "tone": tone,
        "status_label": text(lang, label_key),
        "status": card.get("status") or "",
        "reason": card.get("reasons") or "",
        "next_action": scan_next_action(card, lang),
    }


def _nav_links(lang, active, month):
    # type: (str, str, str) -> Tuple[str, str]
    top = []
    tabs = []
    for key, base, label_key in _NAV:
        if key == "report":
            href = "/report/%s/view?lang=%s" % (month, lang)
        else:
            href = page_href(base, month, lang)
        current = " aria-current='page'" if key == active else ""
        cls = " class='is-active'" if key == active else ""
        label = esc(text(lang, label_key))
        top.append("<a href='%s'%s%s>%s</a>" % (esc(href), cls, current, label))
        tabs.append("<a href='%s'%s%s>%s</a>" % (esc(href), cls, current, label))
    return "".join(top), "".join(tabs)


def _month_options(lang, months, selected):
    # type: (str, List[str], str) -> str
    parts = []
    for name in months:
        selected_attr = " selected" if name == selected else ""
        parts.append(
            "<option value='%s'%s>%s</option>"
            % (esc(name), selected_attr, esc(month_title(lang, name)))
        )
    return "".join(parts)


def _lang_href(path, month, lang):
    # type: (str, str, str) -> str
    if path.startswith("/report/") and "/view" in path:
        return "%s?lang=%s" % (path.split("?")[0], lang)
    base = path.split("?")[0] or "/"
    return page_href(base, month, lang)


def render_page(lang, active, month, months, title, body, path, wide=False):
    # type: (str, str, str, List[str], str, str, str, bool) -> str
    top, tabs = _nav_links(lang, active, month)
    pt_href = _lang_href(path, month, "pt")
    en_href = _lang_href(path, month, "en")
    html_lang = "pt-PT" if lang == "pt" else "en"
    page = _TEMPLATE
    replacements = {
        "__HTML_LANG__": html_lang,
        "__LANG__": esc(lang),
        "__TITLE__": esc(title),
        "__COMPANY__": esc(company_name()),
        "__MONTH_LABEL__": esc(text(lang, "month_label")),
        "__MONTH_OPTIONS__": _month_options(lang, months, month),
        "__MONTH_NAV__": "report" if active == "report" else "query",
        "__LANG_LABEL__": esc(text(lang, "lang_label")),
        "__LANG_PT_HREF__": esc(pt_href),
        "__LANG_EN_HREF__": esc(en_href),
        "__LANG_PT_CLASS__": "is-active" if lang == "pt" else "",
        "__LANG_EN_CLASS__": "is-active" if lang == "en" else "",
        "__LANG_PT_CURRENT__": "aria-current='true'" if lang == "pt" else "",
        "__LANG_EN_CURRENT__": "aria-current='true'" if lang == "en" else "",
        "__NAV_LABEL__": esc(text(lang, "nav_label")),
        "__TOPNAV__": top,
        "__TABBAR__": tabs,
        "__MAIN_CLASS__": "wide" if wide else "",
        "__CONTENT__": body,
    }
    for key, value in replacements.items():
        page = page.replace(key, value)
    return page


def html_response(content, lang):
    # type: (str, str) -> object
    from fastapi.responses import HTMLResponse

    response = HTMLResponse(content)
    response.set_cookie(
        "lang",
        lang,
        max_age=60 * 60 * 24 * 365,
        path="/",
        samesite="lax",
    )
    return response


def parse_note_date(raw):
    # type: (Optional[str]) -> Optional[date]
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None

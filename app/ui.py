"""Shared page chrome: header, language, and the navigation bar."""
import html
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import List, Optional, Tuple

from sqlmodel import Session, select

from app.i18n import text
from app.models import BankLine, Period
from app.money import to_money
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

_TOP = (
    ("dashboard", "/", "nav_home"),
    ("agenda", "/agenda", "nav_agenda"),
    ("pending", "/pending", "nav_pending"),
    ("report", "", "nav_report"),
    ("scan", "/scan", "nav_scan"),
)
_TABS = (
    ("dashboard", "/", "nav_home"),
    ("agenda", "/agenda", "nav_agenda"),
    ("scan", "/scan", "nav_scan"),
    ("pending", "/pending", "nav_pending"),
    ("report", "", "nav_report"),
)
_PT_DAY = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")
_EN_DAY = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_PT_MON = (
    "jan",
    "fev",
    "mar",
    "abr",
    "mai",
    "jun",
    "jul",
    "ago",
    "set",
    "out",
    "nov",
    "dez",
)
_EN_MON = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_CAMERA = (
    "<svg class='nav-icon' viewBox='0 0 24 24' width='26' height='26' aria-hidden='true' "
    "fill='none' stroke='currentColor' stroke-width='2' stroke-linejoin='round'>"
    "<path d='M4 8h3l2-2h6l2 2h3v11H4z'></path>"
    "<circle cx='12' cy='13' r='3.2'></circle></svg>"
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
    """Keep the month argument so older callers still work. Pages are not filtered by it."""
    return _with_lang(path, lang)


def _with_lang(path, lang):
    # type: (str, str) -> str
    base, _, query = (path or "/").partition("?")
    if not base:
        base = "/"
    params = []
    for part in query.split("&"):
        if not part or part.startswith("lang=") or part.startswith("month="):
            continue
        params.append(part)
    params.append("lang=%s" % lang)
    return "%s?%s" % (base, "&".join(params))


def report_href(month, lang):
    # type: (str, str) -> str
    return "/report/%s/view?lang=%s" % (month, lang)


def _as_date(value):
    # type: (object) -> Optional[date]
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = "" if value is None else str(value)
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def show_date(lang, iso):
    # type: (str, object) -> str
    found = _as_date(iso)
    if found is None:
        return "" if iso is None else str(iso)
    months = _PT_MON if lang == "pt" else _EN_MON
    return "%d %s %d" % (found.day, months[found.month - 1], found.year)


def weekday_date(lang, value, today=None):
    # type: (str, object, Optional[date]) -> str
    found = _as_date(value)
    if found is None:
        return ""
    if today is None:
        today = date.today()
    days = _PT_DAY if lang == "pt" else _EN_DAY
    months = _PT_MON if lang == "pt" else _EN_MON
    label = "%s %d %s" % (days[found.weekday()], found.day, months[found.month - 1])
    if found.year != today.year:
        label = "%s %d" % (label, found.year)
    return label


def relative_day(lang, value, today=None):
    # type: (str, object, Optional[date]) -> str
    found = _as_date(value)
    if found is None:
        return ""
    if today is None:
        today = date.today()
    delta = (found - today).days
    if delta == 0:
        return text(lang, "rel_today")
    if delta == 1:
        return text(lang, "rel_tomorrow")
    if delta == -1:
        return text(lang, "rel_yesterday")
    if delta > 1:
        return text(lang, "rel_in_days", n=delta)
    return text(lang, "rel_ago_days", n=-delta)


def relative_time(lang, iso_utc, now=None):
    # type: (str, object, Optional[datetime]) -> str
    raw = "" if iso_utc is None else str(iso_utc).replace("Z", "")
    try:
        stamp = datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return ""
    if now is None:
        now = datetime.utcnow()
    seconds = int((now - stamp).total_seconds())
    if seconds < 45:
        return text(lang, "rel_now")
    minutes = seconds // 60
    if minutes < 60:
        return text(lang, "rel_min", n=max(minutes, 1))
    hours = seconds // 3600
    if hours < 24:
        if hours == 1:
            return text(lang, "rel_hour")
        return text(lang, "rel_hours", n=hours)
    days = seconds // 86400
    if days == 1:
        return text(lang, "rel_day")
    return text(lang, "rel_days", n=days)


def long_date(lang, value):
    # type: (str, date) -> str
    return show_date(lang, value)


def month_title(lang, month):
    # type: (str, str) -> str
    return month_name_year(lang, month)


def month_name_year(lang, month):
    # type: (str, str) -> str
    year_text, _, month_text = (month or "").partition("-")
    try:
        index = int(month_text)
    except ValueError:
        return month
    if index < 1 or index > 12:
        return month
    name = text(lang, "month_%d" % index)
    return "%s %s" % (name, year_text)


def _grouped_whole(amount):
    # type: (Decimal) -> Tuple[str, str, bool]
    negative = amount < 0
    quantized = to_money(abs(amount))
    text_amount = format(quantized, "f")
    whole_text, _, frac = text_amount.partition(".")
    groups = []
    digits = whole_text
    while digits:
        groups.append(digits[-3:])
        digits = digits[:-3]
    groups.reverse()
    return " ".join(groups), frac or "00", negative


def show_amount(lang, value):
    # type: (str, object) -> str
    raw = "" if value is None else str(value).strip()
    if raw in ("", "amount to be confirmed"):
        return text(lang, "amount_unknown")
    try:
        amount = to_money(raw)
    except Exception:
        return text(lang, "amount_unknown")
    grouped, frac, negative = _grouped_whole(amount)
    sign = "-" if negative or raw.startswith("-") else ""
    if lang == "pt":
        core = "%s,%s €" % (grouped, frac)
    else:
        core = "%s.%s €" % (grouped.replace(" ", ","), frac)
    return "%s%s" % (sign, core)


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
    from app.present import plain_reason

    tone, label_key = chip_parts(card.get("status"), card.get("category"))
    total = card.get("total")
    shown_total = show_amount(lang, total) if total not in (None, "") else "—"
    reason = plain_reason(lang, card.get("reasons") or "")
    return {
        "document_type": document_type_label(lang, card.get("document_type")),
        "entity": card.get("entity") or "—",
        "total": shown_total,
        "date": show_date(lang, card.get("date")) or "—",
        "tone": tone,
        "status_label": text(lang, label_key),
        "status": card.get("status") or "",
        "reason": reason,
        "next_action": scan_next_action(card, lang),
    }


def _one_link(lang, active, month, key, base, label_key, scan=False):
    # type: (str, str, str, str, str, str, bool) -> str
    if key == "report":
        href = report_href(month, lang)
    else:
        href = page_href(base, month, lang)
    current = " aria-current='page'" if key == active else ""
    classes = []
    if key == active:
        classes.append("is-active")
    if scan:
        classes.append("nav-scan")
    cls = (" class='%s'" % " ".join(classes)) if classes else ""
    label = esc(text(lang, label_key))
    if scan:
        body = "%s<span class='nav-text'>%s</span>" % (_CAMERA, label)
    else:
        body = "<span class='nav-text'>%s</span>" % label
    return "<a href='%s'%s%s>%s</a>" % (esc(href), cls, current, body)


def _nav_links(lang, active, month):
    # type: (str, str, str) -> Tuple[str, str]
    top = []
    tabs = []
    for key, base, label_key in _TOP:
        top.append(_one_link(lang, active, month, key, base, label_key, key == "scan"))
    for key, base, label_key in _TABS:
        tabs.append(_one_link(lang, active, month, key, base, label_key, key == "scan"))
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


def _other_lang_href(path, lang):
    # type: (str, str) -> Tuple[str, str]
    other = "en" if lang == "pt" else "pt"
    return other.upper(), _with_lang(path, other)


def render_page(lang, active, month, months, title, body, path, wide=False):
    # type: (str, str, str, List[str], str, str, str, bool) -> str
    top, tabs = _nav_links(lang, active, month)
    other_label, other_href = _other_lang_href(path, lang)
    other_code = "en" if lang == "pt" else "pt-PT"
    html_lang = "pt-PT" if lang == "pt" else "en"
    page = _TEMPLATE
    replacements = {
        "__HTML_LANG__": html_lang,
        "__LANG__": esc(lang),
        "__TITLE__": esc(title),
        "__COMPANY__": esc(company_name()),
        "__LANG_LABEL__": esc(text(lang, "lang_label")),
        "__LANG_OTHER_HREF__": esc(other_href),
        "__LANG_OTHER__": esc(other_label),
        "__LANG_OTHER_CODE__": esc(other_code),
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

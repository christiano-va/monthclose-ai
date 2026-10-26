"""Printable monthly report. Code computes every figure. The model only writes prose."""
import hashlib
import html
import json
import logging
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import Session, select

from app.complete import links_support_line
from app.db import ROOT
from app.llm import LLMError, llm_json
from app.models import BankLine, Category, Document, Link, OpenItem, Period
from app.money import ZERO, money_equal, money_or_unknown, to_money
from app.pending import credit_applications_for_month, resolved_after_close
from app.posting import (
    CATEGORY_SUSPECT,
    TIPO_RECEITA,
    TIPO_SUSPECT,
    counts_as_cost,
    excluded_from_totals,
    note_value,
    risk_flag_names,
    suspect_flags,
)
from app.i18n import text
from app.settings import company_name
from app.ui import esc, long_date, month_status_label, month_title, render_page, show_date

logger = logging.getLogger("app.report")

CACHE_DIR = ROOT / "data" / "cache" / "report_summaries"
PROMPT_VERSION = "2026-10-26-v3"
DUE_WINDOW_DAYS = 30

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

_METHOD_ORDER = (
    "reference",
    "exact",
    "n_to_1",
    "1_to_n",
    "llm",
    "payout",
    "settlement",
    "reimbursement",
    "evidence",
    "late_document",
    "explanation",
)

_SUMMARY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "confidence", "justification"],
    "properties": {
        "summary": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "justification": {"type": "string"},
    },
}

_FIGURE_NOTE = (
    "Revenue and expenses are the sum of document base, net of VAT. "
    "Duplicates and suspect documents are excluded. "
    "Settlements are not revenue and not expenses. "
    "Documents still in review stay in the totals and are listed under alerts. "
    "VAT to pay is VAT on sales minus deductible VAT when that difference is positive."
)


def document_is_suspect(document, category_code):
    # type: (Document, Optional[str]) -> bool
    if document.tipo == TIPO_SUSPECT or category_code == CATEGORY_SUSPECT:
        return True
    return bool(suspect_flags(risk_flag_names(document.notes)))


def _esc(value):
    # type: (object) -> str
    return esc(value)


def _one_line(text):
    # type: (Optional[str]) -> str
    return " ".join(str(text or "").split())


def _month_title(month):
    # type: (str) -> str
    year_text, _, month_text = (month or "").partition("-")
    try:
        index = int(month_text)
    except ValueError:
        return month
    if index < 1 or index > 12:
        return month
    return "%s %s" % (_MONTHS[index - 1], year_text)


def _long_date(value):
    # type: (date) -> str
    return "%d %s %d" % (value.day, _MONTHS[value.month - 1], value.year)


def _money(value):
    # type: (object) -> str
    return str(to_money(value))


def _parse_due(document):
    # type: (Document) -> Optional[date]
    raw = note_value(document.notes, "due_date")
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _categories(session):
    # type: (Session) -> Dict[int, Category]
    found = {}  # type: Dict[int, Category]
    for category in session.exec(select(Category)).all():
        if category.id is not None:
            found[category.id] = category
    return found


def _category_code(categories, document):
    # type: (Dict[int, Category], Document) -> Optional[str]
    if document.category_id is None:
        return None
    category = categories.get(document.category_id)
    if category is None:
        return None
    return category.code


def _bank_balances(lines):
    # type: (List[BankLine]) -> Tuple[Optional[Decimal], Optional[Decimal], Optional[bool]]
    ordered = sorted(lines, key=lambda line: (line.movement_date, line.id or 0))
    if not ordered:
        return None, None, None
    first = ordered[0]
    last = ordered[-1]
    if first.balance is None or last.balance is None:
        return None, None, False
    opening = to_money(to_money(first.balance) - to_money(first.amount))
    closing = to_money(last.balance)
    credits = ZERO
    debits = ZERO
    for line in ordered:
        if line.credit is not None:
            credits += to_money(line.credit)
        if line.debit is not None:
            debits += to_money(line.debit)
    computed = to_money(opening + credits - debits)
    return opening, closing, money_equal(computed, closing)


def _json_money(value):
    # type: (Optional[Decimal]) -> Optional[str]
    if value is None:
        return None
    return str(to_money(value))


def collect_report(session, month, today=None):
    # type: (Session, str, Optional[date]) -> Dict[str, Any]
    """Ledger figures for one month, plus the rows each report section prints."""
    if today is None:
        today = date.today()
    period = session.exec(select(Period).where(Period.month == month)).first()
    status = period.status if period is not None and period.status else "open"
    categories = _categories(session)
    documents = session.exec(
        select(Document).where(Document.period == month).order_by(Document.id)
    ).all()
    lines = session.exec(
        select(BankLine).where(BankLine.period == month).order_by(BankLine.movement_date, BankLine.id)
    ).all()
    links = session.exec(select(Link)).all()
    grouped = {}  # type: Dict[int, List[Link]]
    for link in links:
        grouped.setdefault(link.bank_line_id, []).append(link)
    items = session.exec(
        select(OpenItem).where(OpenItem.period == month).order_by(OpenItem.id)
    ).all()

    revenue = ZERO
    expenses = ZERO
    vat_out = ZERO
    vat_in = ZERO
    buckets = {}  # type: Dict[str, Dict[str, Any]]
    suspects = []  # type: List[Dict[str, Any]]
    reviews = []  # type: List[Dict[str, Any]]
    dues = []  # type: List[Dict[str, Any]]
    window_end = today + timedelta(days=DUE_WINDOW_DAYS)

    for document in documents:
        code = _category_code(categories, document)
        suspect = document_is_suspect(document, code)
        if suspect:
            flags = suspect_flags(risk_flag_names(document.notes))
            if not flags and document.tipo == TIPO_SUSPECT:
                flags = ["suspect"]
            suspects.append(
                {
                    "date": document.document_date.isoformat(),
                    "entity": document.entity,
                    "number": document.document_number,
                    "total": _money(document.total),
                    "flags": ", ".join(flags) if flags else "suspect",
                    "justification": _one_line(document.justification),
                }
            )
        elif document.status == "review":
            reviews.append(
                {
                    "date": document.document_date.isoformat(),
                    "entity": document.entity,
                    "number": document.document_number,
                    "total": _money(document.total),
                    "justification": _one_line(document.justification),
                }
            )
        due = _parse_due(document)
        if (
            due is not None
            and today <= due <= window_end
            and document.status != "duplicate"
            and not suspect
        ):
            dues.append(
                {
                    "due": due.isoformat(),
                    "number": document.document_number,
                    "entity": document.entity,
                    "total": _money(document.total),
                }
            )
        if excluded_from_totals(code, document.notes, document.status):
            continue
        if document.tipo == TIPO_SUSPECT:
            continue
        base = to_money(document.base)
        if document.tipo == TIPO_RECEITA:
            revenue = to_money(revenue + base)
            vat_out = to_money(vat_out + to_money(document.vat))
            continue
        if not counts_as_cost(document.tipo, document.status, code):
            continue
        expenses = to_money(expenses + base)
        vat_in = to_money(vat_in + to_money(document.deductible_vat))
        key = code or "uncategorised"
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {"category": key, "tipo": document.tipo, "amount": ZERO}
            buckets[key] = bucket
        bucket["amount"] = to_money(bucket["amount"] + base)

    category_rows = []
    for bucket in buckets.values():
        category_rows.append(
            {
                "category": bucket["category"],
                "tipo": bucket["tipo"],
                "amount": str(bucket["amount"]),
            }
        )
    category_rows.sort(key=lambda row: (-int(to_money(row["amount"]) * 100), row["category"]))

    vat_net = to_money(vat_out - vat_in)
    if vat_net > ZERO:
        vat_to_pay = str(vat_net)  # type: Optional[str]
        vat_to_recover = None  # type: Optional[str]
    elif vat_net < ZERO:
        vat_to_pay = None
        vat_to_recover = str(to_money(abs(vat_net)))
    else:
        vat_to_pay = "0.00"
        vat_to_recover = None

    opening, closing, equation_ok = _bank_balances(lines)
    linked_lines = []
    unsupported = []
    for line in lines:
        if links_support_line(line, grouped.get(line.id, [])):
            linked_lines.append(line)
        else:
            unsupported.append(
                {
                    "id": line.id,
                    "date": line.movement_date.isoformat(),
                    "amount": _money(line.amount),
                    "description": _one_line(line.description),
                }
            )

    method_counts = {}  # type: Dict[str, int]
    line_ids = set(line.id for line in lines)
    for link in links:
        if link.bank_line_id not in line_ids:
            continue
        method_counts[link.method] = method_counts.get(link.method, 0) + 1
    methods = []
    seen = set()
    for name in _METHOD_ORDER:
        seen.add(name)
        methods.append({"method": name, "count": method_counts.get(name, 0)})
    for name in sorted(method_counts):
        if name not in seen:
            methods.append({"method": name, "count": method_counts[name]})

    open_items = []
    accepted = []
    for item in items:
        row = {
            "id": item.id,
            "kind": item.kind,
            "description": _one_line(item.description),
            "amount": money_or_unknown(item.amount),
            "reason": _one_line(item.approval_reason),
            "approved_by": item.approved_by or "",
            "date": item.approved_at.date().isoformat() if item.approved_at else "",
            "risk": item.kind == "missing_sales_invoice",
        }
        if item.status == "open":
            open_items.append(row)
        elif item.status == "accepted_by_owner":
            accepted.append(row)

    resolved = []
    for row in resolved_after_close(session, month):
        resolved.append(
            {
                "date": row.get("date") or "",
                "number": row.get("document_number") or "",
                "document_id": row.get("document_id"),
                "amount": row.get("amount") or "",
                "item": _one_line(row.get("item")),
            }
        )
    credits = credit_applications_for_month(session, month)
    dues.sort(key=lambda row: (row["due"], row["number"] or ""))

    largest_category = None  # type: Optional[str]
    largest_amount = None  # type: Optional[str]
    if category_rows:
        largest_category = category_rows[0]["category"]
        largest_amount = category_rows[0]["amount"]

    figures = {
        "company": company_name(),
        "month": month,
        "status": status,
        "currency": "EUR",
        "revenue_net": str(revenue),
        "expenses_net": str(expenses),
        "vat_on_sales": str(vat_out),
        "deductible_vat": str(vat_in),
        "vat_to_pay": vat_to_pay,
        "vat_to_recover": vat_to_recover,
        "bank_balance_start": _json_money(opening),
        "bank_balance_end": _json_money(closing),
        "bank_equation_ok": equation_ok,
        "bank_lines_linked": len(linked_lines),
        "bank_lines_total": len(lines),
        "unsupported_bank_lines": len(unsupported),
        "open_pending_items": len(open_items),
        "accepted_by_owner": len(accepted),
        "resolved_after_close": len(resolved),
        "suspect_documents": len(suspects),
        "documents_to_review": len(reviews),
        "credit_notes_applied": len(credits),
        "largest_expense_category": largest_category,
        "largest_expense_amount": largest_amount,
        "expenses_by_category": category_rows,
    }
    detail = {
        "generated": today,
        "methods": methods,
        "unsupported": unsupported,
        "open_items": open_items,
        "accepted": accepted,
        "resolved": resolved,
        "suspects": suspects,
        "reviews": reviews,
        "credits": credits,
        "dues": dues,
        "link_rows": sum(method_counts.values()),
    }
    return {"figures": figures, "detail": detail}


def figures_hash(figures):
    # type: (Dict[str, Any]) -> str
    payload = json.dumps(figures, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    material = PROMPT_VERSION + "\n" + payload
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _cache_path(month, lang):
    # type: (str, str) -> Any
    return CACHE_DIR / ("%s.%s.json" % (month, lang))


def _read_cache(month, lang, digest):
    # type: (str, str, str) -> Optional[Dict[str, Any]]
    path = _cache_path(month, lang)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("figures_hash") != digest or data.get("prompt_version") != PROMPT_VERSION:
        return None
    summary = (data.get("summary") or "").strip()
    if not summary:
        return None
    return {
        "summary": summary,
        "confidence": data.get("confidence"),
        "justification": _one_line(data.get("justification")),
        "cache": "hit",
    }


def _write_cache(month, lang, digest, summary, confidence, justification):
    # type: (str, str, str, str, object, str) -> None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "prompt_version": PROMPT_VERSION,
        "figures_hash": digest,
        "summary": summary,
        "confidence": confidence,
        "justification": justification,
    }
    _cache_path(month, lang).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _summary_prompt(figures, lang):
    # type: (Dict[str, Any], str) -> str
    blob = json.dumps(figures, ensure_ascii=False, indent=2)
    if lang == "pt":
        audience = (
            "Write 5 to 8 sentences in European Portuguese (pt-PT). "
            "The reader is the dono of a Portuguese micro-company. "
            "Use these words when they fit: fatura, IVA, extrato, pendências, fecho do mês, dono. "
            "Do not use Brazilian spelling."
        )
    else:
        audience = (
            "Write 5 to 8 sentences in plain English. "
            "The reader is the owner of a Portuguese micro-company."
        )
    return (
        "Write the executive summary for the monthly report.\n"
        "%s\n"
        "Use only the figures in the JSON. They were computed by code.\n"
        "Do not add, subtract, multiply, divide, or round.\n"
        "Do not state a profit, a margin, a result, or any difference.\n"
        "If you mention an amount, copy it exactly as written in the JSON, with the same decimals.\n"
        "If a value is null, leave it out. Do not replace it with zero and do not say it is unavailable.\n"
        "If vat_to_pay is a number, say that VAT is to pay. If vat_to_recover is a number, say that VAT is to recover. Mention only the one that is present.\n"
        "Mention the month, whether it is open or closed, revenue, expenses, the VAT position, "
        "the bank balances, and how many bank lines and pending items still need attention.\n"
        "Write as you would to the owner, in ordinary sentences, not as a list of field names.\n"
        "You may name the largest expense category. Do not list every category.\n\n"
        "Figures:\n%s"
    ) % (audience, blob)


def executive_summary(month, figures, lang="pt"):
    # type: (str, Dict[str, Any], str) -> Dict[str, Any]
    """Return the cached prose for this language when the figures hash matches."""
    digest = figures_hash(figures)
    cached = _read_cache(month, lang, digest)
    if cached is not None:
        logger.info("report summary cache hit %s %s", month, lang)
        return cached
    try:
        data = llm_json(_summary_prompt(figures, lang), _SUMMARY_SCHEMA)
    except LLMError:
        logger.error("report summary failed")
        return {
            "summary": "",
            "confidence": None,
            "justification": "",
            "cache": "error",
        }
    summary = (data.get("summary") or "").strip()
    if not summary:
        return {
            "summary": "",
            "confidence": None,
            "justification": "",
            "cache": "error",
        }
    confidence = data.get("confidence")
    justification = _one_line(data.get("justification"))
    _write_cache(month, lang, digest, summary, confidence, justification)
    return {
        "summary": summary,
        "confidence": confidence,
        "justification": justification,
        "cache": "miss",
    }


def _paragraphs(text):
    # type: (str) -> List[str]
    chunks = []  # type: List[str]
    for part in re.split(r"\n+", text or ""):
        cleaned = part.strip()
        if cleaned:
            chunks.append(cleaned)
    return chunks


def _figure_rows(figures, lang):
    # type: (Dict[str, Any], str) -> List[Tuple[str, str]]
    labels = (
        "revenue_net",
        "expenses_net",
        "vat_on_sales",
        "deductible_vat",
        "vat_to_pay",
        "vat_to_recover",
        "bank_balance_start",
        "bank_balance_end",
        "bank_equation_ok",
        "bank_lines_linked",
        "bank_lines_total",
        "unsupported_bank_lines",
        "open_pending_items",
        "accepted_by_owner",
        "resolved_after_close",
        "suspect_documents",
        "documents_to_review",
        "credit_notes_applied",
        "largest_expense_category",
        "largest_expense_amount",
    )
    rows = []  # type: List[Tuple[str, str]]
    for key in labels:
        value = figures.get(key)
        if value is None:
            shown = "—"
        elif value is True:
            shown = text(lang, "word_yes")
        elif value is False:
            shown = text(lang, "word_no")
        else:
            shown = str(value)
        rows.append((text(lang, "fig_%s" % key), shown))
    return rows


def _empty_row(colspan, text):
    # type: (int, str) -> str
    return "<tr><td colspan='%d'>%s</td></tr>" % (colspan, _esc(text))


def _table(headers, rows_html):
    # type: (List[str], str) -> str
    head = "".join("<th>%s</th>" % _esc(header) for header in headers)
    return "<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (head, rows_html)


def render_monthly_report(session, month, lang="pt", months=None, today=None):
    # type: (Session, str, str, Optional[List[str]], Optional[date]) -> str
    packed = collect_report(session, month, today=today)
    figures = packed["figures"]
    detail = packed["detail"]
    written = executive_summary(month, figures, lang)
    body = _render_html(figures, detail, written, lang)
    if not months:
        months = [month]
    title = "%s — %s" % (text(lang, "report_kicker"), month_title(lang, month))
    return render_page(lang, "report", month, months, title, body, "/report/%s/view" % month, wide=True)


def _render_html(figures, detail, written, lang):
    # type: (Dict[str, Any], Dict[str, Any], Dict[str, Any], str) -> str
    month = figures["month"]
    title = month_title(lang, month)
    raw_status = figures.get("status") or "open"
    status = month_status_label(lang, raw_status)
    generated = long_date(lang, detail["generated"])
    company = figures.get("company") or ""
    running = "%s · %s · %s" % (company, title, status)

    summary_bits = []
    for paragraph in _paragraphs(written.get("summary") or ""):
        summary_bits.append("<p>%s</p>" % _esc(paragraph))
    if not summary_bits:
        summary_bits.append("<p>%s</p>" % _esc(text(lang, "report_summary_failed")))
    confidence = written.get("confidence")
    justification = written.get("justification") or ""
    if confidence is not None or justification:
        summary_bits.append(
            "<p class='quiet'>%s</p>"
            % _esc(
                text(
                    lang,
                    "report_confidence",
                    confidence="" if confidence is None else confidence,
                    why=justification,
                )
            )
        )
    cache_label = ""
    if written.get("cache") == "hit":
        cache_label = text(lang, "report_cached")
    elif written.get("cache") == "miss":
        cache_label = text(lang, "report_written")
    if cache_label:
        summary_bits.append("<p class='quiet'>%s</p>" % _esc(cache_label))

    figure_html = []
    for label, value in _figure_rows(figures, lang):
        figure_html.append(
            "<tr><th>%s</th><td>%s</td></tr>" % (_esc(label), _esc(value))
        )
    for row in figures.get("expenses_by_category") or []:
        figure_html.append(
            "<tr><th>%s</th><td>%s</td></tr>"
            % (
                _esc(text(lang, "fig_category", name=row["category"], tipo=row["tipo"])),
                _esc(row["amount"]),
            )
        )

    category_rows = []
    for row in figures.get("expenses_by_category") or []:
        category_rows.append(
            "<tr><td>%s</td><td>%s</td><td class='num'>%s</td></tr>"
            % (_esc(row["category"]), _esc(row["tipo"]), _esc(row["amount"]))
        )
    if not category_rows:
        category_rows.append(_empty_row(3, text(lang, "report_no_expenses")))

    method_rows = []
    for row in detail["methods"]:
        method_rows.append(
            "<tr><td>%s</td><td class='num'>%s</td></tr>"
            % (_esc(row["method"]), _esc(row["count"]))
        )
    unsupported_rows = []
    for row in detail["unsupported"]:
        unsupported_rows.append(
            "<tr><td>%s</td><td>%s</td><td class='num'>%s</td><td>%s</td></tr>"
            % (_esc(row["id"]), _esc(show_date(lang, row["date"])), _esc(row["amount"]), _esc(row["description"]))
        )
    if not unsupported_rows:
        unsupported_rows.append(_empty_row(4, text(lang, "report_all_supported")))

    def _pending_rows(rows, with_reason):
        # type: (List[Dict[str, Any]], bool) -> str
        built = []
        for row in rows:
            extra = ""
            if with_reason:
                extra = "<td>%s</td><td>%s</td><td>%s</td>" % (
                    _esc(row["reason"] or "—"),
                    _esc(row["approved_by"] or "—"),
                    _esc(row["date"] or "—"),
                )
            risk = ""
            if row.get("risk"):
                risk = text(lang, "report_legal")
            built.append(
                "<tr><td>%s</td><td>%s%s</td><td class='num'>%s</td>%s</tr>"
                % (
                    _esc(row["kind"]),
                    _esc(row["description"]),
                    _esc(risk),
                    _esc(row["amount"]),
                    extra,
                )
            )
        return "".join(built)

    open_body = _pending_rows(detail["open_items"], False)
    if not open_body:
        open_body = _empty_row(3, text(lang, "report_no_open"))
    accepted_body = _pending_rows(detail["accepted"], True)
    if not accepted_body:
        accepted_body = _empty_row(6, text(lang, "report_no_accepted"))

    resolved_rows = []
    for row in detail["resolved"]:
        number = row["number"] or text(lang, "report_doc_fallback", id=row["document_id"])
        resolved_rows.append(
            "<tr><td>%s</td><td>%s</td><td class='num'>%s</td><td>%s</td></tr>"
            % (_esc(show_date(lang, row["date"])), _esc(number), _esc(row["amount"]), _esc(row["item"]))
        )
    if not resolved_rows:
        resolved_rows.append(_empty_row(4, text(lang, "report_no_resolved")))

    suspect_rows = []
    for row in detail["suspects"]:
        suspect_rows.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td class='num'>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                _esc(show_date(lang, row["date"])),
                _esc(row["entity"]),
                _esc(row["number"]),
                _esc(row["total"]),
                _esc(row["flags"]),
                _esc(row["justification"]),
            )
        )
    if not suspect_rows:
        suspect_rows.append(_empty_row(6, text(lang, "report_no_suspects")))

    review_rows = []
    for row in detail["reviews"]:
        review_rows.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td class='num'>%s</td><td>%s</td></tr>"
            % (
                _esc(show_date(lang, row["date"])),
                _esc(row["entity"]),
                _esc(row["number"]),
                _esc(row["total"]),
                _esc(row["justification"]),
            )
        )
    if not review_rows:
        review_rows.append(_empty_row(5, text(lang, "report_no_reviews")))

    credit_rows = []
    for row in detail["credits"]:
        credit_rows.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td class='num'>%s</td><td class='num'>%s</td><td class='num'>%s</td><td>%s</td></tr>"
            % (
                _esc(show_date(lang, row.get("date") or "")),
                _esc(row.get("credit_note") or ""),
                _esc(row.get("invoice") or ""),
                _esc(row.get("invoice_total") or ""),
                _esc(row.get("credit") or ""),
                _esc(row.get("amount_payable") or ""),
                _esc(row.get("issuer") or ""),
            )
        )
    if not credit_rows:
        credit_rows.append(_empty_row(7, text(lang, "report_no_credits")))

    due_rows = []
    for row in detail["dues"]:
        due_rows.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td class='num'>%s</td></tr>"
            % (_esc(show_date(lang, row["due"])), _esc(row["number"]), _esc(row["entity"]), _esc(row["total"]))
        )
    if not due_rows:
        due_rows.append(_empty_row(4, text(lang, "report_no_due")))

    vat_label = text(lang, "report_vat_pay")
    vat_value = figures.get("vat_to_pay")
    if figures.get("vat_to_recover"):
        vat_label = text(lang, "report_vat_recover")
        vat_value = figures.get("vat_to_recover")
    if vat_value is None:
        vat_value = "—"


    recon = text(
        lang,
        "report_recon_line",
        linked=figures.get("bank_lines_linked"),
        total=figures.get("bank_lines_total"),
        links=detail["link_rows"],
    )
    due_note = text(lang, "report_due_note", start=generated)
    sheets = """
<p class="print-bar no-print"><button type="button" id="print-report">%s</button></p>
<section class="sheet">
  <p class="kicker">%s</p>
  <h1>%s</h1>
  <p class="quiet">%s · %s · %s</p>
  <h2>%s</h2>
  <div class="summary-layout">
    <div>
      %s
      <p class="footer-note">%s</p>
    </div>
    <aside>
      <h3>%s</h3>
      <div class="table-wrap"><table class="figures"><tbody>%s</tbody></table></div>
    </aside>
  </div>
</section>
<section class="sheet">
  <p class="quiet">%s</p>
  <h2>%s</h2>
  <p class="quiet">%s</p>
  <dl class="keys">
    <div><dt>%s</dt><dd>%s</dd></div>
    <div><dt>%s</dt><dd>%s</dd></div>
    <div><dt>%s</dt><dd>%s</dd></div>
    <div><dt>%s</dt><dd>%s</dd></div>
    <div><dt>%s</dt><dd>%s</dd></div>
  </dl>
  <h3>%s</h3>
  <div class="table-wrap">%s</div>
</section>
<section class="sheet">
  <p class="quiet">%s</p>
  <h2>%s</h2>
  <p>%s</p>
  <h3>%s</h3>
  <div class="table-wrap">%s</div>
  <h3>%s</h3>
  <div class="table-wrap">%s</div>
</section>
<section class="sheet">
  <p class="quiet">%s</p>
  <h2>%s</h2>
  <h3>%s</h3>
  <div class="table-wrap">%s</div>
  <h3>%s</h3>
  <div class="table-wrap">%s</div>
  <h3>%s</h3>
  <div class="table-wrap">%s</div>
</section>
<section class="sheet" id="alerts">
  <p class="quiet">%s</p>
  <h2>%s</h2>
  <h3>%s</h3>
  <div class="table-wrap">%s</div>
  <h3>%s</h3>
  <div class="table-wrap">%s</div>
  <h3 id="reviews">%s</h3>
  <div class="table-wrap">%s</div>
</section>
<section class="sheet">
  <p class="quiet">%s</p>
  <h2>%s</h2>
  <p class="quiet">%s</p>
  <div class="table-wrap">%s</div>
</section>
""" % (
        _esc(text(lang, "report_print")),
        _esc(text(lang, "report_kicker")),
        _esc(company),
        _esc(title),
        _esc(status),
        _esc(text(lang, "report_generated", date=generated)),
        _esc(text(lang, "report_summary")),
        "".join(summary_bits),
        _esc(text(lang, "report_ai_note")),
        _esc(text(lang, "report_figures_used")),
        "".join(figure_html),
        _esc(running),
        _esc(text(lang, "report_key")),
        _esc(text(lang, "report_figure_note")),
        _esc(text(lang, "report_revenue")),
        _esc(figures.get("revenue_net")),
        _esc(text(lang, "report_expenses")),
        _esc(figures.get("expenses_net")),
        _esc(vat_label),
        _esc(vat_value),
        _esc(text(lang, "report_bank_start")),
        _esc(figures.get("bank_balance_start") if figures.get("bank_balance_start") is not None else "—"),
        _esc(text(lang, "report_bank_end")),
        _esc(figures.get("bank_balance_end") if figures.get("bank_balance_end") is not None else "—"),
        _esc(text(lang, "report_by_category")),
        _table(
            [text(lang, "th_category"), text(lang, "th_type"), text(lang, "th_amount")],
            "".join(category_rows),
        ),
        _esc(running),
        _esc(text(lang, "report_recon")),
        _esc(recon),
        _esc(text(lang, "report_methods")),
        _table([text(lang, "th_method"), text(lang, "th_links")], "".join(method_rows)),
        _esc(text(lang, "report_unsupported")),
        _table(
            [
                text(lang, "th_line"),
                text(lang, "th_date"),
                text(lang, "th_amount"),
                text(lang, "th_description"),
            ],
            "".join(unsupported_rows),
        ),
        _esc(running),
        _esc(text(lang, "report_pending")),
        _esc(text(lang, "report_open")),
        _table(
            [text(lang, "th_kind"), text(lang, "th_item"), text(lang, "th_amount")],
            open_body,
        ),
        _esc(text(lang, "report_accepted")),
        _table(
            [
                text(lang, "th_kind"),
                text(lang, "th_item"),
                text(lang, "th_amount"),
                text(lang, "th_reason"),
                text(lang, "th_approved"),
                text(lang, "th_date"),
            ],
            accepted_body,
        ),
        _esc(text(lang, "report_resolved_after")),
        _table(
            [
                text(lang, "th_date"),
                text(lang, "th_document"),
                text(lang, "th_amount"),
                text(lang, "th_item"),
            ],
            "".join(resolved_rows),
        ),
        _esc(running),
        _esc(text(lang, "report_alerts")),
        _esc(text(lang, "report_suspects")),
        _table(
            [
                text(lang, "th_date"),
                text(lang, "th_counterparty"),
                text(lang, "th_number"),
                text(lang, "th_total"),
                text(lang, "th_signals"),
                text(lang, "th_why"),
            ],
            "".join(suspect_rows),
        ),
        _esc(text(lang, "report_credits")),
        _table(
            [
                text(lang, "th_date"),
                text(lang, "th_credit"),
                text(lang, "th_invoice"),
                text(lang, "th_invoice_total"),
                text(lang, "th_credit_amount"),
                text(lang, "th_payable"),
                text(lang, "th_issuer"),
            ],
            "".join(credit_rows),
        ),
        _esc(text(lang, "report_reviews")),
        _table(
            [
                text(lang, "th_date"),
                text(lang, "th_counterparty"),
                text(lang, "th_number"),
                text(lang, "th_total"),
                text(lang, "th_why"),
            ],
            "".join(review_rows),
        ),
        _esc(running),
        _esc(text(lang, "report_due")),
        _esc(due_note),
        _table(
            [
                text(lang, "th_due"),
                text(lang, "th_number"),
                text(lang, "th_counterparty"),
                text(lang, "th_total"),
            ],
            "".join(due_rows),
        ),
    )
    return sheets

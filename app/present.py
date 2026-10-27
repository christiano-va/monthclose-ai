"""Display names for the owner. Accounting values stay as they were stored."""
import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from app.i18n import text
from app.models import Category, Document, Link, OpenItem
from app.money import ZERO, money_or_unknown, to_money
from app.posting import (
    CATEGORY_SUSPECT,
    TIPO_SUSPECT,
    note_value,
    risk_flag_names,
    suspect_flags,
)
from app.ui import esc, relative_day, show_amount, show_date, weekday_date

_CATEGORY_CODES = (
    "FSE_RENDA",
    "FSE_COMBUSTIVEL",
    "FSE_COMISSOES_PLATAFORMA",
    "FSE_COMUNICACAO",
    "FSE_DESLOCACOES",
    "FSE_ELETRICIDADE_AL",
    "FSE_HONORARIOS",
    "FSE_RENTING_VIATURA",
    "FSE_SEGUROS",
    "FSE_SERVICOS_BANCARIOS",
    "FSE_SOFTWARE_CLOUD",
    "MATERIAS_3D",
    "EQUIPAMENTO_3D",
    "EQUIPAMENTO_ADMIN",
    "OUTROS_DONATIVOS",
    "PESSOAL_ENCARGOS",
    "PESSOAL_REMUNERACOES",
    "REC_3D",
    "REC_AL",
    "REC_CONSULTORIA",
    "ESTADO_IRC",
    "ESTADO_IRC_PPC",
    "ESTADO_IVA",
    "ESTADO_RETENCOES_IRS",
    "ESTADO_SEGURANCA_SOCIAL",
    "SETTLEMENT",
    "SOCIO_CONTA_CORRENTE",
    "SUSPECT",
)

_EXACT_REASONS = {
    "Withholding is a liability; the document total is the gross amount.": "reason_withholding",
    "Credit note is negative and reduces a later invoice.": "reason_credit_item",
    "No eligible document matched this bank line.": "reason_no_document",
    "Cash or personal-card receipt is stored and waits for a bank line.": "reason_awaiting_bank",
    "Hours approved; invoice still to issue.": "reason_hours",
    "Email has no PDF.": "reason_no_pdf",
    "The guest sales invoice was not issued. Accepted so this month can close. Issuing the invoice remains a legal obligation.": "reason_sale_accepted",
    "Lost invoice: the domain renewal document cannot be obtained.": "reason_lost_invoice",
    "Unidentified transfer: the supporting document cannot be obtained.": "reason_unidentified",
    "The document is lost or cannot be obtained. Owner accepts this item so the month can close.": "reason_owner_accept",
    "Main item is a meal-card top-up, not the smaller loading fee.": "reason_meal_card",
    "Loading commission booked apart from the meal-card top-up.": "reason_meal_fee",
    "history status pago / guia_mes_seguinte": "reason_social_due",
    "history status em_aberto / transferencia": "reason_invoice_open",
    "open item from history or inbox": "reason_default",
    "amount to be confirmed": "amount_unknown",
}

_AGENDA_KINDS = (
    "payable",
    "receivable",
    "invoice_to_request",
    "missing_document",
    "missing_sales_invoice",
    "confirm_late_document",
    "confirm_explanation",
    "awaiting_bank_line",
)

_TSU_RE = re.compile(r"(?i)\btsu\b|\bentidade\b")
_DATE_TOKEN = re.compile(r"\d{4}-\d{2}")
_MONEY_IN_TEXT = re.compile(r"(?<![\d.,])(\d+\.\d{2})(?![\d])")
_META_SENTENCE = re.compile(
    r"(?i)(created\s+\d+\s*[- ]\s*sentence\s+summary|"
    r"\bconfidence\b\s*[:=]|"
    r"\bjustification\b\s*[:=]|"
    r"write\s+the\s+executive|"
    r"do\s+not\s+mention|"
    r"these\s+instructions)"
)


def category_label(lang, code, description=""):
    # type: (str, object, object) -> str
    """Friendly category name. Known codes use the dictionary, not the raw code."""
    token = str(code or "").strip()
    key = "cat_%s" % token
    label = text(lang, key)
    if label != key:
        return label
    desc = str(description or "").strip()
    if desc and desc != token and " " in desc and not _looks_english(desc):
        return desc
    if token:
        return text(lang, "cat_other")
    return text(lang, "cat_other")


def tipo_label(lang, tipo):
    # type: (str, object) -> str
    token = str(tipo or "").strip()
    key = "tipo_%s" % token
    label = text(lang, key)
    if label == key:
        return ""
    return label


def method_label(lang, method):
    # type: (str, object) -> str
    token = str(method or "").strip()
    key = "method_%s" % token
    label = text(lang, key)
    if label == key:
        return token
    return label


def flag_label(lang, name):
    # type: (str, object) -> str
    token = str(name or "").strip()
    key = "flag_%s" % token
    label = text(lang, key)
    if label == key:
        return token
    return label


def flag_list(lang, raw):
    # type: (str, object) -> str
    parts = []  # type: List[str]
    for piece in re.split(r"[,;]", str(raw or "")):
        name = piece.strip()
        if not name:
            continue
        label = flag_label(lang, name)
        if label not in parts:
            parts.append(label)
    return ", ".join(parts)


def replace_codes(lang, value):
    # type: (str, object) -> str
    found = str(value or "")
    for code in sorted(_CATEGORY_CODES, key=len, reverse=True):
        if code in found:
            found = found.replace(code, category_label(lang, code))
    return found


def _looks_english(value):
    # type: (str) -> bool
    lowered = value.lower()
    hints = (
        " the ",
        " and ",
        " for ",
        " with ",
        "invoice",
        "owner",
        "withholding",
        "liability",
        "document",
        "payable",
        "review",
    )
    return any(hint in " %s " % lowered for hint in hints)


_SIGNAL_PHRASES = (
    ("counterparty matches the line or the task", "reason_party_matches"),
    ("date is near the bank line", "reason_date_near"),
    ("document type is compatible", "reason_type_ok"),
    ("amount matches", "reason_amount_matches"),
)


def _signal_sentence(lang, incoming):
    # type: (str, str) -> str
    rest = incoming
    found = []  # type: List[str]
    for phrase, key in _SIGNAL_PHRASES:
        if phrase in rest:
            found.append(text(lang, key))
            rest = rest.replace(phrase, " ")
    rest = re.sub(r"[^0-9A-Za-zÀ-ÿ]+", " ", rest).strip()
    if found and not rest:
        return " ".join(found)
    return ""


def plain_reason(lang, raw):
    # type: (str, object) -> str
    """Translate a known rule. Free text from the model is kept, in its own language."""
    incoming = " ".join(str(raw or "").split())
    if not incoming:
        return ""
    if incoming in _EXACT_REASONS:
        return text(lang, _EXACT_REASONS[incoming])
    if incoming.lower().startswith("owner:"):
        incoming = incoming.split(":", 1)[1].strip()
    late = re.match(r"Late resolution of (.+?) by ([^.]+)\.\s*(.*)", incoming)
    if late:
        return text(
            lang,
            "reason_late_resolution",
            item=plain_reason(lang, late.group(1)),
            document=late.group(2).strip(),
            why=plain_reason(lang, late.group(3)),
        )
    bank_line = re.match(
        r"bank line \d+ on (\d{4}-\d{2}-\d{2}) \(([\d.]+) (?:incoming|outgoing)\) "
        r"has no supporting document\. Probably missing a document for (.+?)\.?$",
        incoming,
        re.IGNORECASE,
    )
    if bank_line:
        return text(
            lang,
            "reason_bank_line_missing",
            amount=show_amount(lang, bank_line.group(2)),
            date=show_date(lang, bank_line.group(1)),
            party=bank_line.group(3).strip().rstrip("."),
        )
    line_sum = re.match(
        r"Document (.+?) equals the sum of (\d+) bank lines of the same counterparty\.?",
        incoming,
    )
    if line_sum:
        return text(
            lang,
            "reason_line_sum",
            document=line_sum.group(1).strip(),
            count=line_sum.group(2),
        )
    signals = _signal_sentence(lang, incoming)
    if signals:
        return signals
    matched = re.match(r"Extraction confidence \d+(?:\.\d+)?\.?\s*(.*)", incoming)
    if matched:
        rest = matched.group(1).strip()
        if not rest:
            return text(lang, "reason_low_confidence")
        return plain_reason(lang, rest)
    matched = re.match(
        r"Risk flags ([^;]+);\s*no accounting entry, kept for review\.?",
        incoming,
    )
    if matched:
        return text(lang, "reason_risk_flags", flags=flag_list(lang, matched.group(1)))
    matched = re.match(
        r"Issuer NIF \S+ found in history documents; reused category (\S+)",
        incoming,
    )
    if matched:
        return text(
            lang,
            "reason_known_supplier",
            category=category_label(lang, matched.group(1)),
        )
    if incoming.startswith("Payout booking") or incoming.startswith("Payout lists"):
        return text(lang, "reason_payout_missing")
    lowered = incoming.lower()
    if "hours are approved" in lowered or "requests invoice issuance" in lowered:
        return text(lang, "reason_hours")
    if "; " in incoming or incoming.startswith(
        (
            "low confidence",
            "validation failure",
            "reasons:",
            "unknown category",
            "VAT ",
            "to review",
        )
    ):
        shown = []  # type: List[str]
        for part in re.split(r";\s*", incoming):
            piece = _plain_piece(lang, part.strip())
            if piece and piece not in shown:
                shown.append(piece)
        if shown:
            return " ".join(shown)
    piece = _plain_piece(lang, incoming)
    if piece == incoming:
        return replace_codes(lang, incoming)
    return piece


def _plain_piece(lang, part):
    # type: (str, str) -> str
    if not part or part.startswith("VAT by rate"):
        return ""
    if part in _EXACT_REASONS:
        return text(lang, _EXACT_REASONS[part])
    if part.startswith("low confidence"):
        return text(lang, "reason_low_confidence")
    if part.startswith("validation failure"):
        return text(lang, "reason_validation")
    if part.startswith("foreign supplier"):
        return text(lang, "reason_foreign")
    if part.startswith("unknown category"):
        return text(lang, "reason_unknown_category")
    if part.startswith("SUSPECT not applied") or "fictional footer" in part:
        return text(lang, "reason_suspect_rule")
    if part.startswith("VAT not deductible"):
        return text(lang, "reason_vat_nif")
    if "restaurant VAT" in part:
        return text(lang, "reason_restaurant_vat")
    if "fuel VAT" in part:
        return text(lang, "reason_fuel_vat")
    if part.startswith("credit note stored negative"):
        return text(lang, "reason_credit_negative")
    if part.startswith("review required"):
        return text(lang, "reason_review_missing")
    if part.startswith("no bank line yet"):
        return text(lang, "reason_awaiting_bank")
    if part.startswith("risk_flag:"):
        return flag_list(lang, part.split(":", 1)[1])
    if part.startswith("to review:"):
        return _plain_piece(lang, part.split(":", 1)[1].strip())
    if part.startswith("history status"):
        if "guia_mes_seguinte" in part:
            return text(lang, "reason_social_due")
        if "em_aberto" in part:
            return text(lang, "reason_invoice_open")
    return part


def strip_summary_meta(summary):
    # type: (object) -> str
    """Drop confidence lines and notes about how the summary was written."""
    kept = []  # type: List[str]
    for paragraph in re.split(r"\n+", str(summary or "")):
        sentences = re.split(r"(?<=[.!?])\s+", paragraph.strip())
        clean = []  # type: List[str]
        for sentence in sentences:
            if not sentence or _META_SENTENCE.search(sentence):
                continue
            clean.append(sentence)
        if clean:
            kept.append(" ".join(clean))
    return "\n".join(kept).strip()


def polish_summary(lang, summary):
    # type: (str, object) -> str
    cleaned = replace_codes(lang, strip_summary_meta(summary))

    def _money(match):
        # type: (Any) -> str
        return show_amount(lang, match.group(1))

    return _MONEY_IN_TEXT.sub(_money, cleaned)


def _person_from_tsu(description):
    # type: (str) -> str
    raw = description or ""
    if "—" in raw:
        raw = raw.split("—", 1)[0]
    raw = _TSU_RE.sub(" ", raw)
    raw = _DATE_TOKEN.sub(" ", raw)
    return " ".join(raw.replace("—", " ").split())


def item_heading(lang, kind, notes, description, entity):
    # type: (str, object, object, object, object) -> Dict[str, str]
    """What and to whom, from the kind and the stored fields. Not the English sentence."""
    notes_text = notes or ""
    who = str(entity or "").strip()
    kind_name = str(kind or "")
    action = ""
    if "credit_balance:" in notes_text or "credit_applies:" in notes_text:
        return {
            "what": text(lang, "title_credit"),
            "who": who,
            "action": text(lang, "next_credit"),
        }
    if "withholding_liability:" in notes_text:
        return {
            "what": text(lang, "title_irs"),
            "who": text(lang, "party_tax"),
            "action": text(lang, "next_pay"),
        }
    if kind_name == "invoice_to_request":
        party = note_value(notes_text, "counterparty") or who
        hours = note_value(notes_text, "hours") or ""
        if hours and party:
            party = text(lang, "who_hours", who=party, hours=hours)
        return {
            "what": text(lang, "title_invoice"),
            "who": party,
            "action": text(lang, "next_issue_invoice"),
        }
    if kind_name == "missing_sales_invoice":
        guest = note_value(notes_text, "guest") or who
        if guest == "unknown":
            guest = ""
        return {
            "what": text(lang, "title_missing_sale"),
            "who": guest,
            "action": text(lang, "next_issue_invoice"),
        }
    if kind_name == "missing_document":
        party = note_value(notes_text, "counterparty") or ""
        if not party:
            party = note_value(notes_text, "description") or who
        return {
            "what": text(lang, "title_missing_doc"),
            "who": party,
            "action": text(lang, "next_find_document"),
        }
    if kind_name == "awaiting_bank_line":
        return {
            "what": text(lang, "title_awaiting"),
            "who": who,
            "action": text(lang, "next_bank"),
        }
    if kind_name in ("confirm_late_document", "confirm_explanation"):
        return {
            "what": text(lang, "title_confirm"),
            "who": who,
            "action": text(lang, "next_confirm"),
        }
    if kind_name == "receivable":
        return {
            "what": text(lang, "title_collect"),
            "who": who,
            "action": text(lang, "next_collect"),
        }
    if kind_name == "payable":
        person = _person_from_tsu(str(description or ""))
        social = "segurança social" in who.lower() or "TSU" in str(description or "")
        if social:
            return {
                "what": text(lang, "title_social"),
                "who": person or who or text(lang, "title_social"),
                "action": text(lang, "next_pay"),
            }
        return {
            "what": text(lang, "title_pay"),
            "who": who,
            "action": action or text(lang, "next_pay"),
        }
    from app.ui import kind_label

    return {"what": kind_label(lang, kind_name), "who": who, "action": text(lang, "next_review")}


def is_suspect_document(document, category_code):
    # type: (Document, Optional[str]) -> bool
    if document.tipo == TIPO_SUSPECT or category_code == CATEGORY_SUSPECT:
        return True
    return bool(suspect_flags(risk_flag_names(document.notes)))


def _settled(document, links):
    # type: (Optional[Document], List[Link]) -> bool
    """True when bank links already cover this document. A side liability is handled apart."""
    if document is None:
        return False
    if document.status == "duplicate":
        return True
    if not links:
        return False
    allocated = ZERO
    for link in links:
        allocated += to_money(link.allocated_amount)
    total = to_money(abs(document.total or ZERO))
    if total == ZERO:
        return allocated > ZERO
    return allocated + Decimal("0.02") >= total


def _due_of_document(document):
    # type: (Document) -> Optional[date]
    raw = note_value(document.notes, "due_date")
    if not raw:
        return None
    try:
        from datetime import datetime

        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _bucket(due, today):
    # type: (Optional[date], date) -> str
    if due is None:
        return "later"
    week_end = today + timedelta(days=6 - today.weekday())
    if due < today:
        return "late"
    if due <= week_end:
        return "week"
    return "later"


def _row(lang, what, who, action, amount, due, today, kind):
    # type: (str, str, str, str, object, Optional[date], date, str) -> Dict[str, Any]
    shown_amount = show_amount(lang, amount)
    if str(amount).startswith("-"):
        shown_amount = show_amount(lang, str(amount)[1:])
    when = ""
    relative = ""
    if due is not None:
        when = weekday_date(lang, due, today)
        relative = relative_day(lang, due, today)
    return {
        "what": what,
        "who": who,
        "action": action,
        "amount": shown_amount,
        "when": when,
        "relative": relative,
        "due": due.isoformat() if due else "",
        "bucket": _bucket(due, today),
        "kind": kind,
    }


def agenda_rows(session, lang, today=None):
    # type: (Session, str, Optional[date]) -> List[Dict[str, Any]]
    """Everything the owner still has to pay or do. A settled bank line drops off."""
    if today is None:
        today = date.today()
    documents = {}  # type: Dict[int, Document]
    for document in session.exec(select(Document)).all():
        if document.id is not None:
            documents[document.id] = document
    categories = {}  # type: Dict[int, Category]
    for category in session.exec(select(Category)).all():
        if category.id is not None:
            categories[category.id] = category
    links_by_doc = {}  # type: Dict[int, List[Link]]
    for link in session.exec(select(Link)).all():
        links_by_doc.setdefault(link.document_id, []).append(link)
    items = session.exec(select(OpenItem).where(OpenItem.status == "open")).all()
    rows = []  # type: List[Dict[str, Any]]
    seen_docs = set()  # type: set
    for item in items:
        document = documents.get(item.document_id) if item.document_id else None
        notes = item.notes or ""
        credit = "credit_balance:" in notes
        withholding = "withholding_liability:" in notes
        links = links_by_doc.get(item.document_id or -1, [])
        if (
            not credit
            and not withholding
            and item.kind in ("payable", "receivable")
            and _settled(document, links)
        ):
            continue
        if item.kind not in _AGENDA_KINDS and item.due_date is None and not credit:
            continue
        entity = document.entity if document is not None else ""
        heading = item_heading(lang, item.kind, notes, item.description, entity)
        amount = money_or_unknown(item.amount)
        if credit and str(amount).startswith("-"):
            amount = str(amount)[1:]
        due = item.due_date
        rows.append(
            _row(
                lang,
                heading["what"],
                heading["who"],
                heading["action"],
                amount,
                due,
                today,
                "credit" if credit else (item.kind or ""),
            )
        )
        if item.document_id:
            seen_docs.add(item.document_id)
    for document in documents.values():
        if document.id in seen_docs:
            continue
        if document.status in ("duplicate", "error", "pago"):
            continue
        category = categories.get(document.category_id) if document.category_id else None
        code = category.code if category is not None else ""
        if is_suspect_document(document, code):
            continue
        due = _due_of_document(document)
        if due is None:
            continue
        links = links_by_doc.get(document.id or -1, [])
        if _settled(document, links):
            continue
        if document.tipo == "liquidacao":
            kind = "payable"
            heading = item_heading(lang, kind, document.notes, document.document_number, document.entity)
        elif document.direction == "sale" or document.tipo == "receita":
            kind = "receivable"
            heading = item_heading(lang, kind, "", document.document_number, document.entity)
        else:
            kind = "payable"
            heading = item_heading(lang, kind, document.notes, document.document_number, document.entity)
        rows.append(
            _row(
                lang,
                heading["what"],
                heading["who"],
                heading["action"],
                money_or_unknown(document.total),
                due,
                today,
                kind,
            )
        )
    rows.sort(key=lambda row: (row["due"] or "9999-99-99", row["what"], row["who"]))
    return rows


def agenda_groups(rows):
    # type: (List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]
    groups = {"late": [], "week": [], "later": []}  # type: Dict[str, List[Dict[str, Any]]]
    for row in rows:
        groups[row["bucket"]].append(row)
    groups["late"].sort(key=lambda row: row["due"] or "", reverse=True)
    groups["week"].sort(key=lambda row: row["due"] or "")
    groups["later"].sort(key=lambda row: (row["due"] or "9999-99-99", row["what"]))
    return groups


def home_preview(rows):
    # type: (List[Dict[str, Any]]) -> List[Dict[str, Any]]
    """The next three things: this week first, then the closest overdue items."""
    groups = agenda_groups(rows)
    chosen = list(groups["week"])
    if len(chosen) < 3:
        chosen.extend(groups["late"])
    if len(chosen) < 3:
        chosen.extend(groups["later"])
    return chosen[:3]


def render_agenda_row(row, compact=False):
    # type: (Dict[str, Any], bool) -> str
    late = " agenda-late" if row.get("bucket") == "late" else ""
    when = ""
    if row.get("when"):
        relative = row.get("relative") or ""
        extra = " · %s" % relative if relative else ""
        when = "<p class='agenda-when'>%s%s</p>" % (esc(row["when"]), esc(extra))
    action = ""
    if not compact and row.get("action"):
        action = "<p class='agenda-next'>%s</p>" % esc(row["action"])
    who = ""
    if row.get("who"):
        who = "<p class='agenda-who'>%s</p>" % esc(row["who"])
    return (
        "<li class='agenda-row%s'><div class='agenda-main'><p class='agenda-what'>%s</p>%s%s</div>"
        "<div class='agenda-side'><p class='agenda-amount'>%s</p>%s</div></li>"
        % (late, esc(row.get("what") or ""), who, action, esc(row.get("amount") or ""), when)
    )


def attention_counts(session):
    # type: (Session) -> Dict[str, int]
    categories = {}  # type: Dict[int, str]
    for category in session.exec(select(Category)).all():
        if category.id is not None:
            categories[category.id] = category.code
    review = 0
    suspect = 0
    for document in session.exec(select(Document)).all():
        code = categories.get(document.category_id)
        if document.status == "review":
            review += 1
        if is_suspect_document(document, code):
            suspect += 1
    pending = len(session.exec(select(OpenItem).where(OpenItem.status == "open")).all())
    return {"pending": pending, "review": review, "suspect": suspect}

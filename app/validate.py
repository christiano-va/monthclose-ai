"""Code checks on extracted documents. The LLM proposes; this module decides."""
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.money import ZERO, money_equal, to_money
from app.settings import FINAL_CONSUMER_NIF

ALLOWED_VAT_RATES = (
    Decimal("0"),
    Decimal("6"),
    Decimal("13"),
    Decimal("23"),
)

# Fixed risk-flag names. Anything else the model returns is ignored.
# A sender-domain mismatch is not a flag: the mailbox receives mail
# forwarded by the accountant and by the owner.
RISK_FLAGS = (
    "iban_changed",
    "iban_new",
    "urgency_language",
    "short_payment_deadline",
    "personal_account",
)

DOCUMENT_KINDS = (
    "invoice",
    "receipt",
    "payslip",
    "tax_guide",
    "payment_proof",
    "payment_notice",
    "bank_statement",
    "payout_report",
    "other",
)

VAT_CHECK_KINDS = ("invoice", "receipt")
PAYSLIP_KIND = "payslip"
AMOUNT_DATE_KINDS = (
    "tax_guide",
    "payment_proof",
    "payment_notice",
    "payout_report",
    "other",
)

# Fewer than this many days between the document date and the due date
# is an unusually short payment deadline. A normal 6-10 day term is not.
SHORT_PAYMENT_DAYS = 5

FOREIGN_SUPPLIER_NOTE = "foreign supplier - reverse charge, review"

_KIND_ALIASES = {
    "invoice": "invoice",
    "fatura": "invoice",
    "factura": "invoice",
    "fatura-recibo": "invoice",
    "fatura_recibo": "invoice",
    "nota_credito": "invoice",
    "nota_de_credito": "invoice",
    "credit_note": "invoice",
    "nota_debito": "invoice",
    "debit_note": "invoice",
    "receipt": "receipt",
    "recibo": "receipt",
    "payslip": "payslip",
    "recibo_vencimento": "payslip",
    "vencimento": "payslip",
    "tax_guide": "tax_guide",
    "guia": "tax_guide",
    "payment_proof": "payment_proof",
    "comprovativo": "payment_proof",
    "payment_notice": "payment_notice",
    "aviso": "payment_notice",
    "aviso_pagamento": "payment_notice",
    "bank_statement": "bank_statement",
    "extrato": "bank_statement",
    "payout_report": "payout_report",
    "other": "other",
}

_URGENCY_MARKERS = (
    "urgente",
    "urgent",
    "suspens",
    "imediatamente",
    "last chance",
    "última oportunidade",
    "ultima oportunidade",
    "de imediato",
)

_IBAN_CHANGE_MARKERS = (
    "iban foi alter",
    "iban alterad",
    "alterámos o nosso iban",
    "alteramos o nosso iban",
    "alterámos o iban",
    "alteramos o iban",
    "novo iban",
    "new iban",
    "iban atualiz",
    "iban actualiz",
    "mudança de iban",
    "mudanca de iban",
    "mudança do iban",
    "mudanca do iban",
    "dados bancários alter",
    "dados bancarios alter",
    "bank details changed",
    "bank details have changed",
    "updated our iban",
    "iban was changed",
    "iban has changed",
)

_PERSONAL_MARKERS = (
    "conta pessoal",
    "conta particular",
    "iban pessoal",
    "personal account",
    "private account",
    "meios pessoais",
)

_SHORT_DEMAND_MARKERS = (
    "pagamento urgente",
    "pagar hoje",
    "até amanhã",
    "ate amanha",
    "dentro de 24",
    "nas próximas 24",
    "nas proximas 24",
    "imediatamente",
    "de imediato",
)

_IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}(?:[ \t]?\d){11,34})\b")


def parse_money_field(value):
    # type: (Any) -> Decimal
    if value is None or value == "":
        return ZERO
    if isinstance(value, (int, float, Decimal)):
        return to_money(value)
    text = str(value).strip().replace("%", "").replace(" ", "")
    if not text:
        return ZERO
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(".", "").replace(",", ".")
    return to_money(text)


def parse_iso_date(value):
    # type: (Any) -> Optional[date]
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()[:10]
    return datetime.strptime(text, "%Y-%m-%d").date()


def portuguese_nif_valid(nif):
    # type: (Optional[str]) -> bool
    if not nif:
        return False
    digits = "".join(ch for ch in str(nif) if ch.isdigit())
    if len(digits) != 9:
        return False
    # 999999990 is the final-consumer NIF. It passes mod-11 and is accepted.
    if digits == FINAL_CONSUMER_NIF:
        return True
    weights = (9, 8, 7, 6, 5, 4, 3, 2)
    total = 0
    for index, weight in enumerate(weights):
        total += int(digits[index]) * weight
    remainder = total % 11
    check = 0 if remainder < 2 else 11 - remainder
    return check == int(digits[8])


def classify_nif(nif):
    # type: (Optional[str]) -> Tuple[str, str]
    """Return (pt|foreign|absent, normalized id). Zeros are not an issuer NIF."""
    if nif is None:
        return "absent", ""
    raw = str(nif).strip().upper()
    if raw in ("", "-", "N/A", "NA", "NONE", "NULL"):
        return "absent", ""
    alnum = "".join(ch for ch in raw if ch.isalnum())
    if not alnum or set(alnum) <= set("0"):
        return "absent", ""
    if alnum.startswith("PT") and len(alnum) == 11 and alnum[2:].isdigit():
        return "pt", alnum[2:]
    if len(alnum) == 9 and alnum.isdigit():
        return "pt", alnum
    return "foreign", alnum


def normalize_document_kind(value):
    # type: (Optional[str]) -> str
    text = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    if text in DOCUMENT_KINDS:
        return text
    if text in _KIND_ALIASES:
        return _KIND_ALIASES[text]
    return "other"


def normalize_iban(value):
    # type: (Optional[str]) -> str
    if not value:
        return ""
    compact = "".join(ch for ch in str(value).upper() if ch.isalnum())
    if len(compact) < 15 or not compact[:2].isalpha() or not compact[2:4].isdigit():
        return ""
    return compact


def find_ibans(text):
    # type: (Optional[str]) -> List[str]
    if not text:
        return []
    found = []  # type: List[str]
    for match in _IBAN_RE.finditer(str(text).upper()):
        iban = normalize_iban(match.group(1))
        if iban and iban not in found:
            found.append(iban)
    return found


def resolve_risk_flags(
    proposed,
    text,
    previous_ibans,
    issuer_known,
    document_kind,
    doc_date,
    due_date,
    payment_iban,
):
    # type: (Any, str, Sequence[str], bool, str, Optional[date], Optional[date], Optional[str]) -> List[str]
    """Keep only the fixed flags, and only when the text or prior IBANs support them.

    A normal payment IBAN on a known supplier is not a flag. Sender domain is never a flag.
    """
    # `proposed` is the model list. Names outside RISK_FLAGS, including a
    # sender-domain mismatch, are never copied. Flags below come from the
    # text and from IBANs already stored for this issuer.
    if proposed is None:
        proposed = []

    blob = (text or "").lower()
    flags = []  # type: List[str]

    current = normalize_iban(payment_iban)
    previous = []  # type: List[str]
    for item in previous_ibans or []:
        iban = normalize_iban(item)
        if iban and iban not in previous:
            previous.append(iban)

    announces_change = any(marker in blob for marker in _IBAN_CHANGE_MARKERS)
    same_as_previous = bool(current and previous and current in previous)
    # Payslips, receipts and bank proofs print the worker's or the company's
    # own account. Only an invoice or a reimbursement note can show a new
    # supplier account.
    iban_relevant = document_kind in ("invoice", "other")
    if iban_relevant and issuer_known and not same_as_previous:
        if current and previous and current not in previous:
            flags.append("iban_changed")
        elif announces_change:
            flags.append("iban_changed" if previous else "iban_new")

    if any(marker in blob for marker in _URGENCY_MARKERS):
        flags.append("urgency_language")

    demands_short = any(marker in blob for marker in _SHORT_DEMAND_MARKERS)
    urgency = any(marker in blob for marker in _URGENCY_MARKERS)
    if document_kind == "invoice" and doc_date is not None and due_date is not None:
        delta = (due_date - doc_date).days
        # A due date a few days out, with no pressure wording, is a normal term.
        if 0 <= delta < SHORT_PAYMENT_DAYS and (demands_short or urgency):
            flags.append("short_payment_deadline")

    personal_hit = any(marker in blob for marker in _PERSONAL_MARKERS) or (
        "reembolso" in blob and "iban" in blob
    )
    if personal_hit:
        flags.append("personal_account")

    ordered = []  # type: List[str]
    for name in RISK_FLAGS:
        if name in flags:
            ordered.append(name)
    return ordered


def _vat_lines(extracted):
    # type: (Dict[str, Any]) -> List[Tuple[Decimal, Decimal, Decimal]]
    lines = []
    raw_lines = extracted.get("vat_per_rate") or []
    for item in raw_lines:
        rate = parse_money_field(item.get("rate"))
        base = parse_money_field(item.get("base"))
        vat = parse_money_field(item.get("vat"))
        lines.append((rate, base, vat))
    return lines


def parse_vat_rate(value):
    # type: (Any) -> Decimal
    text = str(value or "").strip().lower()
    if text in ("rc", "reverse charge", "isento", "isent", "m07"):
        return Decimal("0")
    return parse_money_field(value)


def line_items(extracted):
    # type: (Dict[str, Any]) -> List[Dict[str, Any]]
    """Parsed lines. nature is supply, fee, topup, tax or other."""
    parsed = []
    for item in extracted.get("lines") or []:
        if not isinstance(item, dict):
            continue
        nature = str(item.get("nature") or "supply").strip().lower()
        if nature not in ("supply", "fee", "topup", "tax", "other"):
            nature = "other"
        base = parse_money_field(item.get("base"))
        vat = parse_money_field(item.get("vat"))
        total = parse_money_field(item.get("total"))
        # A deduction is often extracted with a positive base and a negative total.
        if vat == ZERO and total < ZERO and base > ZERO:
            base = total
        elif vat == ZERO and base < ZERO and total > ZERO and total == -base:
            total = base
        parsed.append(
            {
                "description": str(item.get("description") or "").strip(),
                "base": base,
                "vat_rate": parse_vat_rate(item.get("vat_rate")),
                "vat": vat,
                "total": total,
                "nature": nature,
            }
        )
    return parsed


def align_line_rounding(lines, payable):
    # type: (List[Dict[str, Any]], Decimal) -> List[Dict[str, Any]]
    """Absorb a one-cent gap caused by rounding VAT on each line."""
    if not lines:
        return lines
    total = ZERO
    for line in lines:
        total += line["total"]
    diff = to_money(payable - total)
    if diff == ZERO or abs(diff) > Decimal("0.01"):
        return lines
    target = lines[0]
    for line in lines:
        if abs(line["vat"]) > abs(target["vat"]):
            target = line
    target["vat"] = to_money(target["vat"] + diff)
    target["total"] = to_money(target["base"] + target["vat"])
    return lines


def total_payable_amount(extracted):
    # type: (Dict[str, Any]) -> Decimal
    raw = extracted.get("total_payable")
    if raw is None or str(raw).strip() == "":
        raw = extracted.get("total")
    if raw is None or str(raw).strip() == "":
        raw = extracted.get("amount")
    return parse_money_field(raw)


def _check_lines(extracted, errors):
    # type: (Dict[str, Any], List[str]) -> Optional[Tuple[Decimal, Decimal, Decimal, List[Dict[str, Any]]]]
    """Validate VAT on each line and sum(lines) == total_payable.

    Returns (base, vat, total_payable, lines) when lines are present.
    """
    lines = []
    try:
        lines = line_items(extracted)
    except (InvalidOperation, ValueError):
        errors.append("lines contain an invalid amount")
        return None
    if not lines:
        return None

    base_sum = ZERO
    vat_sum = ZERO
    total_sum = ZERO
    for index, line in enumerate(lines, start=1):
        label = line["description"] or ("line %d" % index)
        rate = line["vat_rate"]
        if rate not in ALLOWED_VAT_RATES:
            errors.append("line %s VAT rate %s is not in {0, 6, 13, 23}" % (label, rate))
        if not money_equal(line["base"] + line["vat"], line["total"]):
            errors.append(
                "line %s base %s + VAT %s != total %s"
                % (label, line["base"], line["vat"], line["total"])
            )
        if rate == ZERO and line["vat"] != ZERO:
            errors.append("line %s has VAT at rate 0" % label)
        elif rate != ZERO:
            expected = to_money(line["base"] * rate / Decimal("100"))
            if abs(expected - line["vat"]) > Decimal("0.02"):
                errors.append(
                    "line %s VAT %s does not match rate %s on base %s"
                    % (label, line["vat"], rate, line["base"])
                )
        base_sum += line["base"]
        vat_sum += line["vat"]
        total_sum += line["total"]

    try:
        payable = total_payable_amount(extracted)
    except (InvalidOperation, ValueError):
        errors.append("total_payable is not valid money")
        payable = ZERO
    if not money_equal(total_sum, payable):
        errors.append(
            "sum of lines %s != total_payable %s (tolerance 0.01)" % (to_money(total_sum), payable)
        )
    return to_money(base_sum), to_money(vat_sum), to_money(payable), lines


def _optional_money(extracted, key):
    # type: (Dict[str, Any], str) -> Optional[Decimal]
    value = extracted.get(key)
    if value is None or str(value).strip() == "":
        return None
    return parse_money_field(value)


def _check_date(extracted, errors, today):
    # type: (Dict[str, Any], List[str], date) -> Optional[date]
    doc_date = None
    try:
        doc_date = parse_iso_date(extracted.get("date"))
    except (TypeError, ValueError):
        errors.append("document date is not YYYY-MM-DD")
    if doc_date is None and "document date is not YYYY-MM-DD" not in errors:
        errors.append("document date is missing")
    if doc_date is not None:
        earliest = date(2015, 1, 1)
        latest = today + timedelta(days=90)
        if doc_date < earliest or doc_date > latest:
            errors.append("document date %s is outside the sanity window" % doc_date)
    return doc_date


def _check_due_date(extracted, doc_date, errors):
    # type: (Dict[str, Any], Optional[date], List[str]) -> Optional[date]
    due = None
    raw_due = extracted.get("due_date")
    if not raw_due:
        return None
    try:
        due = parse_iso_date(raw_due)
    except (TypeError, ValueError):
        errors.append("due date is not YYYY-MM-DD")
        return None
    if due is not None and doc_date is not None and due > doc_date + timedelta(days=730):
        errors.append("due date is more than two years after the document date")
    return due


def _result(errors, base, vat_sum, total, doc_date, due, nif, kind, foreign_note, deductions):
    # type: (List[str], Decimal, Decimal, Decimal, Optional[date], Optional[date], Optional[str], str, Optional[str], Optional[Decimal]) -> Dict[str, Any]
    return {
        "ok": len(errors) == 0,
        "status": "review" if errors else "validated",
        "errors": errors,
        "base": str(base),
        "vat": str(vat_sum),
        "total": str(total),
        "deductions": str(deductions) if deductions is not None else None,
        "document_date": doc_date.isoformat() if doc_date else None,
        "due_date": due.isoformat() if due else None,
        "nif": nif,
        "document_kind": kind,
        "foreign_note": foreign_note,
    }


def validate_extraction(extracted, today=None):
    # type: (Dict[str, Any], Optional[date]) -> Dict[str, Any]
    """Return {ok, status, errors} using Decimal arithmetic only.

    When lines are present, each line has VAT and sum(lines) must equal total_payable.
    invoice/receipt without lines: base + VAT == total, rate in {0, 6, 13, 23}.
    payslip: gross - deductions == net.
    tax_guide, payment_proof, payment_notice, payout_report, other: amount and date,
    plus the line check when lines are present.
    Portuguese mod-11 runs only for a 9-digit Portuguese NIF. 999999990 is valid.
    """
    if today is None:
        today = date.today()
    errors = []  # type: List[str]
    kind = normalize_document_kind(
        extracted.get("document_kind") or extracted.get("document_type")
    )
    issuer_raw = extracted.get("issuer_nif")
    if issuer_raw in (None, ""):
        issuer_raw = extracted.get("nif")
    nif_kind, nif_norm = classify_nif(issuer_raw)
    foreign_note = FOREIGN_SUPPLIER_NOTE if nif_kind == "foreign" else None

    if nif_kind == "pt" and not portuguese_nif_valid(nif_norm):
        errors.append("issuer NIF failed Portuguese mod-11 checksum")

    counterparty_raw = extracted.get("counterparty_nif")
    if counterparty_raw not in (None, ""):
        cp_kind, cp_norm = classify_nif(counterparty_raw)
        if cp_kind == "pt" and not portuguese_nif_valid(cp_norm):
            errors.append("counterparty NIF failed Portuguese mod-11 checksum")

    if kind == "bank_statement":
        doc_date = None
        try:
            doc_date = parse_iso_date(extracted.get("date"))
        except (TypeError, ValueError):
            doc_date = None
        return _result(errors, ZERO, ZERO, ZERO, doc_date, None, nif_norm or None, kind, foreign_note, None)

    doc_date = _check_date(extracted, errors, today)
    due = None
    if kind in VAT_CHECK_KINDS:
        due = _check_due_date(extracted, doc_date, errors)

    if kind == PAYSLIP_KIND:
        gross = ZERO
        deductions = ZERO
        net = ZERO
        try:
            gross_value = _optional_money(extracted, "gross")
            deductions_value = _optional_money(extracted, "deductions")
            net_value = _optional_money(extracted, "net")
            if net_value is None:
                net_value = _optional_money(extracted, "amount")
            if net_value is None:
                net_value = _optional_money(extracted, "total")
            if gross_value is None or deductions_value is None or net_value is None:
                errors.append("payslip requires gross, deductions and net")
                gross = gross_value or ZERO
                deductions = deductions_value or ZERO
                net = net_value or ZERO
            else:
                gross = gross_value
                deductions = deductions_value
                net = net_value
                if not money_equal(gross - deductions, net):
                    errors.append(
                        "gross %s - deductions %s != net %s (tolerance 0.01)"
                        % (gross, deductions, net)
                    )
        except (InvalidOperation, ValueError):
            errors.append("payslip amounts are not valid money")
        checked = _check_lines(extracted, errors)
        if checked is not None:
            _base, _vat, payable, _lines = checked
            if not money_equal(payable, net):
                errors.append(
                    "sum of lines %s != payslip net %s (tolerance 0.01)" % (payable, net)
                )
        return _result(
            errors, gross, ZERO, net, doc_date, due, nif_norm or None, kind, foreign_note, deductions
        )

    checked_lines = _check_lines(extracted, errors)

    if kind in AMOUNT_DATE_KINDS:
        amount = ZERO
        try:
            amount_value = _optional_money(extracted, "amount")
            if amount_value is None:
                amount_value = _optional_money(extracted, "total")
            if amount_value is None or amount_value == ZERO:
                raw_amount = extracted.get("amount")
                raw_total = extracted.get("total")
                raw_payable = extracted.get("total_payable")
                if (
                    raw_amount in (None, "")
                    and raw_total in (None, "")
                    and raw_payable in (None, "")
                    and checked_lines is None
                ):
                    errors.append("amount is missing")
                elif amount_value is None:
                    errors.append("amount is not valid money")
                else:
                    amount = amount_value
            else:
                amount = amount_value
        except (InvalidOperation, ValueError):
            errors.append("amount is not valid money")
        if checked_lines is not None:
            base, vat_sum, payable, _lines = checked_lines
            return _result(
                errors, base, vat_sum, payable, doc_date, due, nif_norm or None, kind, foreign_note, None
            )
        return _result(
            errors, amount, ZERO, amount, doc_date, due, nif_norm or None, kind, foreign_note, None
        )

    if checked_lines is not None:
        base, vat_sum, payable, _lines = checked_lines
        return _result(
            errors, base, vat_sum, payable, doc_date, due, nif_norm or None, kind, foreign_note, None
        )

    errors.append("lines are missing")

    try:
        base = parse_money_field(extracted.get("base"))
        total = parse_money_field(extracted.get("total"))
    except (InvalidOperation, ValueError):
        errors.append("base/total are not valid money amounts")
        base = ZERO
        total = ZERO

    vat_lines = []  # type: List[Tuple[Decimal, Decimal, Decimal]]
    try:
        vat_lines = _vat_lines(extracted)
    except (InvalidOperation, ValueError):
        errors.append("vat_per_rate contains an invalid amount")

    vat_sum = ZERO
    base_sum = ZERO
    for rate, line_base, line_vat in vat_lines:
        base_sum += line_base
        vat_sum += line_vat
        if rate not in ALLOWED_VAT_RATES:
            errors.append("VAT rate %s is not in {0, 6, 13, 23}" % rate)

    if not vat_lines:
        if money_equal(base, total) and base != ZERO:
            vat_sum = ZERO
        else:
            errors.append("vat_per_rate is empty")

    if vat_lines and not money_equal(base_sum, base):
        errors.append("sum of VAT bases %s != document base %s" % (base_sum, base))

    if not (not vat_lines and "vat_per_rate is empty" in errors):
        if not money_equal(base + vat_sum, total):
            errors.append(
                "base %s + VAT %s != total %s (tolerance 0.01)" % (base, vat_sum, total)
            )

    return _result(
        errors, base, vat_sum, total, doc_date, due, nif_norm or None, kind, foreign_note, None
    )

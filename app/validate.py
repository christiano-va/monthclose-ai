"""Code checks on extracted documents. The LLM proposes; this module decides."""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from app.money import ZERO, money_equal, to_money

ALLOWED_VAT_RATES = (
    Decimal("0"),
    Decimal("6"),
    Decimal("13"),
    Decimal("23"),
)


def parse_money_field(value):
    # type: (Any) -> Decimal
    if value is None or value == "":
        return ZERO
    if isinstance(value, (int, float, Decimal)):
        return to_money(value)
    text = str(value).strip()
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
    weights = (9, 8, 7, 6, 5, 4, 3, 2)
    total = 0
    for index, weight in enumerate(weights):
        total += int(digits[index]) * weight
    remainder = total % 11
    check = 0 if remainder < 2 else 11 - remainder
    return check == int(digits[8])


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


def validate_extraction(extracted, today=None):
    # type: (Dict[str, Any], Optional[date]) -> Dict[str, Any]
    """Return {ok, status, errors} using Decimal arithmetic only."""
    if today is None:
        today = date.today()
    errors = []  # type: List[str]

    try:
        base = parse_money_field(extracted.get("base"))
        total = parse_money_field(extracted.get("total"))
    except (InvalidOperation, ValueError):
        errors.append("base/total are not valid money amounts")
        base = ZERO
        total = ZERO

    vat_lines = []
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
            errors.append(
                "VAT rate %s is not in {0, 6, 13, 23}" % rate
            )

    if not vat_lines:
        errors.append("vat_per_rate is empty")

    if vat_lines and not money_equal(base_sum, base):
        errors.append(
            "sum of VAT bases %s != document base %s" % (base_sum, base)
        )

    if not money_equal(base + vat_sum, total):
        errors.append(
            "base %s + VAT %s != total %s (tolerance 0.01)"
            % (base, vat_sum, total)
        )

    nif = extracted.get("nif")
    if not portuguese_nif_valid(nif):
        errors.append("issuer NIF failed Portuguese mod-11 checksum")

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

    due = None
    raw_due = extracted.get("due_date")
    if raw_due:
        try:
            due = parse_iso_date(raw_due)
        except (TypeError, ValueError):
            errors.append("due date is not YYYY-MM-DD")
        if due is not None and doc_date is not None:
            if due > doc_date + timedelta(days=730):
                errors.append("due date is more than two years after the document date")

    status = "review" if errors else "validated"
    return {
        "ok": len(errors) == 0,
        "status": status,
        "errors": errors,
        "base": str(base),
        "vat": str(vat_sum),
        "total": str(total),
        "document_date": doc_date.isoformat() if doc_date else None,
        "due_date": due.isoformat() if due else None,
        "nif": nif,
    }

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Union

TWOPLACES = Decimal("0.01")
ZERO = Decimal("0.00")


def to_money(value):
    # type: (Union[None, str, int, float, Decimal]) -> Decimal
    """Convert a value to a 2-decimal Decimal. Never use float arithmetic."""
    if value is None or value == "":
        return ZERO
    if isinstance(value, Decimal):
        return value.quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    return Decimal(str(value)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def parse_pt_money(value):
    # type: (Optional[str]) -> Optional[Decimal]
    """Parse Portuguese bank amounts such as '1.012,50' or '0,46'."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    normalized = text.replace(".", "").replace(",", ".")
    return Decimal(normalized).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def money_equal(left, right, tolerance=Decimal("0.01")):
    # type: (Decimal, Decimal, Decimal) -> bool
    return abs(to_money(left) - to_money(right)) <= tolerance

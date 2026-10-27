"""Periods loaded before the first bank statement are archived history.

Their open items stay in the database with status settled_by_history.
Agenda, Pendências and the Início counters only use the statement month
and later months.
"""
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from app.models import BankLine, OpenItem, Period
from app.posting import set_note

SETTLED_BY_HISTORY = "settled_by_history"


def first_statement_month(session):
    # type: (Session) -> Optional[str]
    """Earliest month that has an imported bank line."""
    months = []  # type: List[str]
    for row in session.exec(select(BankLine.period)).all():
        month = row[0] if isinstance(row, (tuple, list)) else row
        if month and month not in months:
            months.append(month)
    if not months:
        return None
    return min(months)


def is_archived_period(month, cutoff):
    # type: (Optional[str], Optional[str]) -> bool
    """True when this month is before the first imported bank statement."""
    if not cutoff or not month:
        return False
    return month < cutoff


def settle_historical_periods(session):
    # type: (Session) -> Dict[str, Any]
    """Mark open items from archived periods. The rows are kept."""
    cutoff = first_statement_month(session)
    if cutoff is None:
        return {"cutoff": None, "periods": 0, "items": 0}
    marked_periods = 0
    marked_items = 0
    periods = session.exec(select(Period).order_by(Period.month)).all()
    for period in periods:
        if not is_archived_period(period.month, cutoff):
            continue
        already = "settled_by_history:" in (period.notes or "")
        period.notes = set_note(period.notes, "settled_by_history", "yes")
        session.add(period)
        if not already:
            marked_periods += 1
        items = session.exec(
            select(OpenItem).where(OpenItem.period == period.month)
        ).all()
        for item in items:
            if item.status != "open":
                continue
            item.status = SETTLED_BY_HISTORY
            item.notes = set_note(item.notes, "settled_by_history", "yes")
            item.justification = (
                "Archived history: this period is before the first imported bank statement."
            )
            session.add(item)
            marked_items += 1
    session.commit()
    return {"cutoff": cutoff, "periods": marked_periods, "items": marked_items}

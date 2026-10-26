from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import Session, select

from app.models import BankLine, Period
from app.money import ZERO, parse_pt_money, to_money


def parse_bank_statement_csv(text):
    # type: (str) -> Tuple[Dict[str, Optional[str]], List[dict]]
    """Parse a Banco Exemplo semicolon CSV (Portuguese amounts and dates)."""
    lines = text.splitlines()
    meta = {
        "bank_name": None,
        "account_iban": None,
        "period_label": None,
        "opening_balance": None,
        "closing_balance": None,
    }
    rows = []  # type: List[dict]
    in_table = False

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(";")]

        if not in_table:
            if line.startswith("Banco ") or (len(parts) == 1 and "Extrato" in line):
                meta["bank_name"] = line
                continue
            if parts[0].startswith("Conta") and len(parts) >= 2:
                meta["account_iban"] = parts[1]
                continue
            if parts[0].lower().startswith("per") and len(parts) >= 2:
                meta["period_label"] = parts[1]
                continue
            if parts[0].startswith("Saldo inicial") and len(parts) >= 2:
                opening = parse_pt_money(parts[1])
                meta["opening_balance"] = (
                    str(opening) if opening is not None else None
                )
                continue
            if parts[0].startswith("Data Mov"):
                in_table = True
                continue
            continue

        if parts[0].startswith("Saldo final"):
            closing = parse_pt_money(parts[1] if len(parts) > 1 else "")
            meta["closing_balance"] = str(closing) if closing is not None else None
            continue

        if len(parts) < 4:
            continue

        movement_date = datetime.strptime(parts[0], "%d-%m-%Y").date()
        value_date = None
        if len(parts) > 1 and parts[1]:
            value_date = datetime.strptime(parts[1], "%d-%m-%Y").date()
        description = parts[2] if len(parts) > 2 else ""
        debit = parse_pt_money(parts[3] if len(parts) > 3 else "")
        credit = parse_pt_money(parts[4] if len(parts) > 4 else "")
        balance = parse_pt_money(parts[5] if len(parts) > 5 else "")

        debit_amt = debit if debit is not None else ZERO
        credit_amt = credit if credit is not None else ZERO
        amount = to_money(credit_amt - debit_amt)

        rows.append(
            {
                "period": movement_date.strftime("%Y-%m"),
                "account_iban": meta["account_iban"],
                "movement_date": movement_date,
                "value_date": value_date,
                "description": description,
                "debit": debit,
                "credit": credit,
                "amount": amount,
                "balance": balance,
            }
        )

    return meta, rows


def import_statement_text(session, text, justification=None):
    # type: (Session, str, Optional[str]) -> Dict[str, Any]
    """Insert bank lines from statement CSV text. Existing lines are skipped."""
    if justification is None:
        justification = "imported from bank statement CSV"
    meta, rows = parse_bank_statement_csv(text)
    created = 0
    skipped = 0
    periods = set()

    for row in rows:
        month = row["period"]
        period = session.exec(select(Period).where(Period.month == month)).first()
        if period is None:
            session.add(Period(month=month, status="open"))
            session.commit()
        periods.add(month)
        existing = session.exec(
            select(BankLine).where(
                BankLine.movement_date == row["movement_date"],
                BankLine.description == row["description"],
                BankLine.amount == row["amount"],
            )
        ).first()
        if existing is not None:
            skipped += 1
            continue
        line = BankLine(
            period=row["period"],
            account_iban=row["account_iban"],
            movement_date=row["movement_date"],
            value_date=row["value_date"],
            description=row["description"],
            debit=row["debit"],
            credit=row["credit"],
            amount=row["amount"],
            balance=row["balance"],
            status="unsupported",
            confidence=1.0,
            justification=justification,
        )
        session.add(line)
        created += 1

    session.commit()
    return {
        "meta": meta,
        "created": created,
        "skipped": skipped,
        "periods": sorted(periods),
    }

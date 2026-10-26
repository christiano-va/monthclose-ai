import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlmodel import Session, select

from app.db import ROOT, get_session, init_db
from app.models import (
    BankLine,
    BankStatementImport,
    Category,
    Document,
    DocumentCreate,
    Link,
    LinkCreate,
    OpenItem,
    Period,
)
from app.money import money_equal, to_money
from app.statements import parse_bank_statement_csv

MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

app = FastAPI(title="MonthClose AI", version="0.1.0")


@app.on_event("startup")
def on_startup():
    init_db()


def require_month(month):
    # type: (str) -> str
    if not MONTH_RE.match(month):
        raise HTTPException(
            status_code=400,
            detail="month must be YYYY-MM",
        )
    return month


def get_or_create_period(session, month):
    # type: (Session, str) -> Period
    require_month(month)
    period = session.exec(select(Period).where(Period.month == month)).first()
    if period is None:
        period = Period(month=month, status="open")
        session.add(period)
        session.commit()
        session.refresh(period)
    return period


def get_or_create_category(session, code, snc_account=None):
    # type: (Session, str, Optional[str]) -> Category
    category = session.exec(select(Category).where(Category.code == code)).first()
    if category is None:
        category = Category(code=code, snc_account=snc_account, description=code)
        session.add(category)
        session.commit()
        session.refresh(category)
        return category
    if snc_account and not category.snc_account:
        category.snc_account = snc_account
        session.add(category)
        session.commit()
        session.refresh(category)
    return category


def refresh_bank_line_status(session, bank_line):
    # type: (Session, BankLine) -> BankLine
    link = session.exec(
        select(Link).where(Link.bank_line_id == bank_line.id)
    ).first()
    bank_line.status = "linked" if link is not None else "unsupported"
    session.add(bank_line)
    return bank_line


def unsupported_bank_lines(session, month):
    # type: (Session, str) -> List[BankLine]
    lines = session.exec(select(BankLine).where(BankLine.period == month)).all()
    result = []
    for line in lines:
        link = session.exec(
            select(Link).where(Link.bank_line_id == line.id)
        ).first()
        if link is None:
            result.append(line)
    return result


@app.get("/")
def root():
    return {"name": "MonthClose AI", "docs": "/docs"}


@app.post("/documents", response_model=Document)
def create_document(
    payload: DocumentCreate, session: Session = Depends(get_session)
) -> Document:
    period_month = payload.period or payload.document_date.strftime("%Y-%m")
    get_or_create_period(session, period_month)
    category = get_or_create_category(
        session, payload.category_code, payload.snc_account
    )

    base = to_money(payload.base)
    vat = to_money(payload.vat)
    total = to_money(payload.total)
    if not money_equal(base + vat, total):
        raise HTTPException(
            status_code=400,
            detail="base + vat must equal total (checked in code, not by the model)",
        )

    if payload.source_id:
        existing = session.exec(
            select(Document).where(Document.source_id == payload.source_id)
        ).first()
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail="document with this source_id already exists",
            )

    document = Document(
        source_id=payload.source_id,
        period=period_month,
        document_date=payload.document_date,
        payment_date=payload.payment_date,
        tipo=payload.tipo,
        category_id=category.id,
        entity=payload.entity,
        nif=payload.nif,
        document_number=payload.document_number,
        base=base,
        vat_rate=to_money(payload.vat_rate),
        vat=vat,
        total=total,
        deductible_vat=to_money(payload.deductible_vat),
        payment_method=payload.payment_method,
        status=payload.status,
        notes=payload.notes,
        file_path=payload.file_path,
        drive_link=payload.drive_link,
        confidence=payload.confidence,
        justification=payload.justification,
    )
    session.add(document)
    session.commit()
    session.refresh(document)
    return document


@app.get("/documents", response_model=List[Document])
def list_documents(
    period: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
) -> List[Document]:
    statement = select(Document)
    if period:
        require_month(period)
        statement = statement.where(Document.period == period)
    statement = statement.order_by(Document.document_date, Document.id)
    return session.exec(statement).all()


@app.post("/bank-statements")
def import_bank_statement(
    payload: BankStatementImport, session: Session = Depends(get_session)
) -> dict:
    if not payload.path and not payload.content:
        raise HTTPException(
            status_code=400,
            detail="provide path or content",
        )

    if payload.content:
        text = payload.content
    else:
        csv_path = Path(payload.path)
        if not csv_path.is_absolute():
            csv_path = ROOT / csv_path
        if not csv_path.exists():
            raise HTTPException(status_code=404, detail="CSV file not found")
        raw = csv_path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")

    meta, rows = parse_bank_statement_csv(text)
    created = 0
    skipped = 0
    periods = set()

    for row in rows:
        get_or_create_period(session, row["period"])
        periods.add(row["period"])
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
            justification="imported from bank statement CSV",
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


@app.get("/bank-lines", response_model=List[BankLine])
def list_bank_lines(
    status: Optional[str] = Query(default=None),
    period: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
) -> List[BankLine]:
    statement = select(BankLine)
    if period:
        require_month(period)
        statement = statement.where(BankLine.period == period)
    if status:
        statement = statement.where(BankLine.status == status)
    statement = statement.order_by(BankLine.movement_date, BankLine.id)
    return session.exec(statement).all()


@app.post("/links", response_model=Link)
def create_link(
    payload: LinkCreate, session: Session = Depends(get_session)
) -> Link:
    document = session.get(Document, payload.document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    bank_line = session.get(BankLine, payload.bank_line_id)
    if bank_line is None:
        raise HTTPException(status_code=404, detail="bank line not found")

    duplicate = session.exec(
        select(Link).where(
            Link.document_id == payload.document_id,
            Link.bank_line_id == payload.bank_line_id,
        )
    ).first()
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="link already exists")

    allocated = to_money(payload.allocated_amount)
    if allocated <= Decimal("0.00"):
        raise HTTPException(
            status_code=400,
            detail="allocated_amount must be greater than 0",
        )

    link = Link(
        document_id=payload.document_id,
        bank_line_id=payload.bank_line_id,
        allocated_amount=allocated,
        method=payload.method,
        explanation=payload.explanation,
        confidence=payload.confidence,
    )
    session.add(link)
    refresh_bank_line_status(session, bank_line)
    session.commit()
    session.refresh(link)
    return link


@app.get("/periods/{month}/checklist")
def period_checklist(month: str, session: Session = Depends(get_session)) -> dict:
    require_month(month)
    period = session.exec(select(Period).where(Period.month == month)).first()
    documents = session.exec(
        select(Document).where(Document.period == month)
    ).all()
    bank_lines = session.exec(
        select(BankLine).where(BankLine.period == month)
    ).all()
    unsupported = unsupported_bank_lines(session, month)
    open_items = session.exec(
        select(OpenItem).where(OpenItem.period == month, OpenItem.status == "open")
    ).all()
    links = session.exec(select(Link)).all()
    month_doc_ids = {d.id for d in documents}
    month_line_ids = {b.id for b in bank_lines}
    month_links = [
        ln
        for ln in links
        if ln.document_id in month_doc_ids or ln.bank_line_id in month_line_ids
    ]

    blockers = []
    if unsupported:
        blockers.append(
            "%d bank line(s) have no supporting document" % len(unsupported)
        )
    if period is not None and period.status == "closed":
        blockers.append("period is already closed")

    return {
        "month": month,
        "status": period.status if period is not None else "missing",
        "documents": len(documents),
        "bank_lines": len(bank_lines),
        "unsupported_bank_lines": len(unsupported),
        "unsupported_bank_line_ids": [line.id for line in unsupported],
        "links": len(month_links),
        "open_items": len(open_items),
        "can_close": len(unsupported) == 0
        and period is not None
        and period.status == "open"
        and len(bank_lines) > 0,
        "blockers": blockers,
        "rule": "no bank line without a supporting document",
    }


@app.post("/periods/{month}/close")
def close_period(month: str, session: Session = Depends(get_session)) -> dict:
    require_month(month)
    period = get_or_create_period(session, month)
    if period.status == "closed":
        raise HTTPException(status_code=409, detail="period is already closed")

    bank_lines = session.exec(
        select(BankLine).where(BankLine.period == month)
    ).all()
    if not bank_lines:
        raise HTTPException(
            status_code=409,
            detail="cannot close %s: no bank lines imported for this period"
            % month,
        )

    unsupported = unsupported_bank_lines(session, month)
    if unsupported:
        raise HTTPException(
            status_code=409,
            detail={
                "error": (
                    "Cannot close period %s: every bank line needs a "
                    "supporting document. %d line(s) are still unsupported."
                    % (month, len(unsupported))
                ),
                "month": month,
                "unsupported_count": len(unsupported),
                "unsupported_bank_line_ids": [line.id for line in unsupported],
            },
        )

    period.status = "closed"
    period.closed_at = datetime.utcnow()
    session.add(period)
    session.commit()
    session.refresh(period)
    return {
        "month": period.month,
        "status": period.status,
        "closed_at": period.closed_at.isoformat() if period.closed_at else None,
    }

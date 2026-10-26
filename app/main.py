import re
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select

from app.db import ROOT, get_session, init_db
from app.complete import links_support_line, partial_invoice_balances
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
    PeriodCloseBody,
    ScanImport,
)
from app.pending import credit_applications_for_month, resolved_after_close
from app.money import ZERO, money_equal, money_or_unknown, to_money
from app.posting import excluded_from_linking, note_value
from app.home import document_due, render_home
from app.pending_view import render_pending
from app.report import document_is_suspect, render_monthly_report
from app.ui import html_response, prefers_html, resolve_lang, selected_month
from app.phone import router as phone_router
from app.statements import import_statement_text
from app.sync import SyncError, run_sync, sync_status

# Open tasks that block the month unless the owner accepts them one by one.
BLOCKING_KINDS = ("missing_document", "missing_sales_invoice")

MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

app = FastAPI(title="MonthClose AI", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")
app.include_router(phone_router)


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
    links = session.exec(select(Link).where(Link.bank_line_id == bank_line.id)).all()
    bank_line.status = "linked" if links_support_line(bank_line, links) else "unsupported"
    session.add(bank_line)
    return bank_line


def _links_for_lines(session):
    # type: (Session) -> dict
    grouped = {}
    for link in session.exec(select(Link)).all():
        grouped.setdefault(link.bank_line_id, []).append(link)
    return grouped


def _accepted_gap_line_ids(items):
    # type: (List[OpenItem]) -> set
    found = set()
    for item in items:
        if item.status != "accepted_by_owner" or item.kind != "missing_document":
            continue
        raw = note_value(item.notes, "bank_line_id")
        if raw and str(raw).isdigit():
            found.add(int(raw))
    return found


def _accepted_payload(item):
    # type: (OpenItem) -> dict
    higher = item.kind == "missing_sales_invoice"
    approved_date = None
    if item.approved_at is not None:
        approved_date = item.approved_at.date().isoformat()
    return {
        "id": item.id,
        "kind": item.kind,
        "status": item.status,
        "item": item.description,
        "amount": str(to_money(item.amount)),
        "reason": item.approval_reason,
        "approved_by": item.approved_by,
        "date": approved_date,
        "risk": "higher" if higher else "normal",
        "risk_note": (
            "Issuing the sales invoice is a legal obligation." if higher else None
        ),
    }


def _blocker_item(item):
    # type: (OpenItem) -> dict
    row = {
        "id": item.id,
        "kind": item.kind,
        "item": item.description,
        "amount": str(to_money(item.amount)),
        "status": item.status,
    }
    guest = note_value(item.notes, "guest")
    booking = note_value(item.notes, "booking")
    if guest:
        row["guest"] = guest
    if booking:
        row["booking"] = booking
    return row


def _blockers_by_kind(unsupported, blocking_items):
    # type: (List[BankLine], List[OpenItem]) -> dict
    by_kind = {
        "unsupported_bank_line": [],
        "missing_document": [],
        "missing_sales_invoice": [],
    }
    for line in unsupported:
        by_kind["unsupported_bank_line"].append(
            {
                "bank_line_id": line.id,
                "date": line.movement_date.isoformat(),
                "amount": str(to_money(line.amount)),
                "description": line.description,
            }
        )
    ordered = sorted(blocking_items, key=lambda item: item.id or 0)
    for item in ordered:
        if item.kind in by_kind:
            by_kind[item.kind].append(_blocker_item(item))
    return by_kind


def period_gaps(session, month):
    # type: (Session, str) -> tuple
    """Unresolved bank lines, open blocking tasks, and accepted pendings."""
    bank_lines = session.exec(
        select(BankLine).where(BankLine.period == month).order_by(BankLine.id)
    ).all()
    items = session.exec(select(OpenItem).where(OpenItem.period == month)).all()
    accepted_lines = _accepted_gap_line_ids(items)
    links = _links_for_lines(session)
    unsupported = []
    for line in bank_lines:
        if links_support_line(line, links.get(line.id, [])):
            continue
        if line.id in accepted_lines:
            continue
        unsupported.append(line)
    blocking = [
        item
        for item in items
        if item.kind in BLOCKING_KINDS and item.status == "open"
    ]
    accepted = [
        item
        for item in items
        if item.kind in BLOCKING_KINDS and item.status == "accepted_by_owner"
    ]
    blocking.sort(key=lambda item: item.id or 0)
    accepted.sort(key=lambda item: (0 if item.kind == "missing_sales_invoice" else 1, item.id or 0))
    return bank_lines, unsupported, blocking, accepted


def _dashboard_payload(session, requested):
    # type: (Session, Optional[str]) -> dict
    selected, months = selected_month(session, requested)
    checklist = period_checklist(selected, session)
    documents = session.exec(select(Document).where(Document.period == selected)).all()
    categories = {}
    for category in session.exec(select(Category)).all():
        if category.id is not None:
            categories[category.id] = category.code
    validated = 0
    review = 0
    suspect = 0
    for document in documents:
        code = categories.get(document.category_id)
        if document_is_suspect(document, code):
            suspect += 1
        if document.status == "validated":
            validated += 1
        elif document.status == "review":
            review += 1
    pending = []
    dues = []
    today = date.today()
    horizon = today.toordinal() + 30
    rows = session.exec(
        select(OpenItem)
        .where(OpenItem.period == selected, OpenItem.status == "open")
        .order_by(OpenItem.id)
    ).all()
    for item in rows:
        pending.append(
            {
                "kind": item.kind,
                "description": item.description,
                "amount": money_or_unknown(item.amount),
                "due": item.due_date.isoformat() if item.due_date else "",
            }
        )
    for document in documents:
        due = document_due(document)
        if due is None or document.status == "duplicate" or due < today:
            continue
        if due.toordinal() > horizon:
            continue
        dues.append(
            {
                "due": due.isoformat(),
                "label": document.entity,
                "amount": money_or_unknown(document.total),
            }
        )
    return {
        "month": selected,
        "months": months,
        "checklist": checklist,
        "documents": len(documents),
        "validated": validated,
        "review": review,
        "suspect": suspect,
        "pending": pending,
        "dues": dues,
        "sync": sync_status(),
    }


@app.get("/")
def root(
    request: Request,
    month: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
):
    if prefers_html(request):
        lang = resolve_lang(request)
        return html_response(render_home(_dashboard_payload(session, month), lang), lang)
    return {"name": "MonthClose AI", "docs": "/docs"}


@app.post("/sync")
def sync_mailbox() -> dict:
    """Read Gmail, process documents, upload to Drive, link, and draft owner mail."""
    try:
        return run_sync()
    except SyncError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.code)


@app.get("/sync/status")
def sync_mailbox_status() -> dict:
    return sync_status()


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
        original_filename=payload.original_filename,
        direction=payload.direction,
        referenced_invoice=payload.referenced_invoice,
        lines_json=payload.lines_json,
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

    return import_statement_text(session, text)


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
    category = (
        session.get(Category, document.category_id) if document.category_id else None
    )
    category_code = category.code if category is not None else None
    if excluded_from_linking(category_code, document.notes, document.status):
        raise HTTPException(
            status_code=409,
            detail=(
                "suspect documents have no accounting entry and are excluded from linking"
            ),
        )
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
    if payload.method == "evidence":
        if allocated != ZERO:
            raise HTTPException(
                status_code=400,
                detail="an evidence link allocates 0.00",
            )
    elif payload.method == "payout":
        if allocated == ZERO:
            raise HTTPException(
                status_code=400,
                detail="a payout allocation carries the gross or the commission",
            )
    elif allocated <= ZERO:
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
    bank_lines, unsupported, blocking, accepted = period_gaps(session, month)
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
    blockers_by_kind = _blockers_by_kind(unsupported, blocking)
    blockers = []
    if unsupported:
        blockers.append(
            "%d bank line(s) have no supporting document" % len(unsupported)
        )
    for kind in BLOCKING_KINDS:
        rows = blockers_by_kind[kind]
        if rows:
            blockers.append("%d open %s task(s)" % (len(rows), kind))
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
        and len(blocking) == 0
        and period is not None
        and period.status == "open"
        and len(bank_lines) > 0,
        "blockers": blockers,
        "blockers_by_kind": blockers_by_kind,
        "accepted_pending_items": [_accepted_payload(item) for item in accepted],
        "resolved_after_close": resolved_after_close(session, month),
        "credit_applications": credit_applications_for_month(session, month),
        "invoice_open_balances": partial_invoice_balances(session),
        "rule": (
            "no bank line without a supporting document; "
            "open missing_document and missing_sales_invoice tasks block closing "
            "until the owner accepts each one"
        ),
    }


def _refuse_close(month, unsupported, blocking):
    # type: (str, List[BankLine], List[OpenItem]) -> None
    raise HTTPException(
        status_code=409,
        detail={
            "error": (
                "Cannot close period %s without owner approval while "
                "bank lines are unsupported or owner tasks are open."
                % month
            ),
            "month": month,
            "blockers_by_kind": _blockers_by_kind(unsupported, blocking),
        },
    )


@app.post("/periods/{month}/close")
def close_period(
    month: str,
    payload: Optional[PeriodCloseBody] = Body(default=None),
    session: Session = Depends(get_session),
) -> dict:
    require_month(month)
    period = get_or_create_period(session, month)
    if period.status == "closed":
        raise HTTPException(status_code=409, detail="period is already closed")

    bank_lines, unsupported, blocking, _accepted = period_gaps(session, month)
    if not bank_lines:
        raise HTTPException(
            status_code=409,
            detail="cannot close %s: no bank lines imported for this period" % month,
        )

    approvals = [] if payload is None else list(payload.approvals)
    if not approvals:
        if unsupported or blocking:
            _refuse_close(month, unsupported, blocking)
    else:
        if payload is None or not (payload.approved_by or "").strip():
            raise HTTPException(status_code=400, detail="approved_by is required")
        if payload.timestamp is None:
            raise HTTPException(status_code=400, detail="timestamp is required")
        reasons = {}
        for decision in approvals:
            reason = (decision.reason or "").strip()
            if not reason:
                raise HTTPException(
                    status_code=400, detail="each approval needs a reason"
                )
            if decision.id in reasons:
                raise HTTPException(
                    status_code=400, detail="duplicate approval id %s" % decision.id
                )
            reasons[decision.id] = reason
        blocking_ids = set(item.id for item in blocking)
        unknown = set(reasons) - blocking_ids
        if unknown:
            raise HTTPException(
                status_code=400,
                detail="approval ids are not open pending items: %s"
                % sorted(unknown),
            )
        if set(reasons) != blocking_ids:
            _refuse_close(month, unsupported, blocking)
        approved_by = payload.approved_by.strip()
        for item in blocking:
            item.status = "accepted_by_owner"
            item.approval_reason = reasons[item.id]
            item.approved_by = approved_by
            item.approved_at = payload.timestamp
            item.justification = reasons[item.id]
            session.add(item)
        session.flush()
        _lines, unsupported, blocking, _accepted = period_gaps(session, month)
        if unsupported or blocking:
            session.rollback()
            _refuse_close(month, unsupported, blocking)

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


@app.get("/pending", response_class=HTMLResponse)
def pending_page(
    request: Request,
    month: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
):
    lang = resolve_lang(request)
    selected, months = selected_month(session, month)
    return html_response(render_pending(session, selected, months, lang), lang)


@app.get("/report/{month}/view", response_class=HTMLResponse)
def monthly_report_view(
    month: str,
    request: Request,
    session: Session = Depends(get_session),
):
    """Printable monthly report. Figures are computed in code; the model writes the summary."""
    require_month(month)
    lang = resolve_lang(request)
    _selected, months = selected_month(session, month)
    return html_response(render_monthly_report(session, month, lang, months), lang)


@app.get("/reports/{month}")
def monthly_report(month: str, session: Session = Depends(get_session)) -> dict:
    require_month(month)
    period = session.exec(select(Period).where(Period.month == month)).first()
    _lines, _unsupported, _blocking, accepted = period_gaps(session, month)
    return {
        "month": month,
        "status": period.status if period is not None else "missing",
        "accepted_pending_items": [_accepted_payload(item) for item in accepted],
        "resolved_after_close": resolved_after_close(session, month),
        "credit_applications": credit_applications_for_month(session, month),
    }


@app.post("/scans")
def import_scan(payload: ScanImport, session: Session = Depends(get_session)) -> dict:
    """Store one scanned PDF. An optional note is context; the sender is not."""
    from app.extract import list_categories
    from app.process_one import process_scan_bytes

    pdf_path = Path(payload.path)
    if not pdf_path.is_absolute():
        pdf_path = ROOT / pdf_path
    if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=404, detail="PDF file not found")
    categories = list_categories(session)
    if not categories:
        raise HTTPException(status_code=409, detail="Category table is empty")
    results = process_scan_bytes(
        session,
        pdf_path.read_bytes(),
        pdf_path.name,
        payload.note or "",
        categories,
    )
    return {"results": results}

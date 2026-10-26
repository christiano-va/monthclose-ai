"""Phone scan page: one photo or PDF, then the same checks as an email."""
import hashlib
import hmac
import json
import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from app.db import engine, get_session
from app.extract import list_categories
from app.i18n import text
from app.models import Category, Document, PhoneScan
from app.money import to_money
from app.posting import note_value
from app.process_one import process_scan_bytes, save_scan_file
from app.settings import scan_code
from app.ui import (
    esc,
    html_response,
    month_title,
    page_href,
    prefers_html,
    present_scan_card,
    render_page,
    resolve_lang,
    selected_month,
)

logger = logging.getLogger("app.phone")
router = APIRouter()

MAX_SCAN_BYTES = 20 * 1024 * 1024
_JOBS = {}  # type: Dict[str, Dict[str, Any]]
_JOBS_LOCK = threading.Lock()

_SCAN_STRINGS = (
    "scan_elapsed",
    "scan_progress",
    "scan_another",
    "see_recent",
    "pending_next",
    "retry",
    "doc_other",
    "step_uploading",
    "step_reading",
    "step_validating",
    "step_matching",
    "step_done",
    "error_bad_code",
    "error_offline",
    "error_unreadable",
    "error_empty",
    "error_too_large",
    "error_bad_type",
    "error_failed",
    "error_not_ready",
    "error_unknown_job",
)


def codes_match(given):
    # type: (str) -> bool
    expected = scan_code()
    if not expected or not (given or "").strip():
        return False
    left = hashlib.sha256(given.strip().encode("utf-8")).hexdigest()
    right = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    return hmac.compare_digest(left, right)


def _allowed(filename, content_type):
    # type: (str, str) -> bool
    name = (filename or "").lower()
    ctype = (content_type or "").lower()
    if ctype.startswith("image/") or ctype == "application/pdf":
        return True
    return name.endswith(
        (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif")
    )


def scan_card(session, item):
    # type: (Session, Dict[str, Any]) -> Dict[str, Any]
    match = item.get("late_match") or {}
    pending = None
    if match.get("outcome") == "linked" and (
        match.get("item") or match.get("open_item_id")
    ):
        pending = {
            "id": match.get("open_item_id"),
            "kind": match.get("item_kind"),
            "description": match.get("item"),
            "status": match.get("item_status") or "resolved_by_late_document",
        }
    document = None
    if item.get("document_id"):
        document = session.get(Document, item["document_id"])
    if document is None:
        return {
            "document_type": item.get("document_kind") or "",
            "entity": "",
            "nif": "",
            "date": "",
            "total": "",
            "vat": "",
            "category": "",
            "status": item.get("status") or "",
            "confidence": item.get("confidence") or 0,
            "reasons": item.get("reason") or "",
            "pending_item": pending,
            "document_id": item.get("document_id"),
            "filename": item.get("attachment") or "",
        }
    category = None
    if document.category_id:
        category = session.get(Category, document.category_id)
    kind = note_value(document.notes, "kind") or item.get("document_kind") or document.tipo
    receipt_kind = note_value(document.notes, "receipt_kind")
    category_label = category.code if category is not None else (receipt_kind or "")
    reasons = item.get("reason") or note_value(document.notes, "reasons") or ""
    code = category.code if category is not None else ""
    if not reasons and (code == "SUSPECT" or document.status in ("review", "error")):
        reasons = "review required: the triggering rule was not recorded"
    return {
        "document_type": kind,
        "entity": document.entity,
        "nif": document.nif or "",
        "date": document.document_date.isoformat() if document.document_date else "",
        "total": str(to_money(document.total)),
        "vat": str(to_money(document.vat)),
        "category": category_label,
        "status": document.status,
        "confidence": document.confidence,
        "reasons": reasons,
        "pending_item": pending,
        "document_id": document.id,
        "filename": document.original_filename or item.get("attachment") or "",
    }


def _record_scan(session, filename, data, cards):
    # type: (Session, str, bytes, List[Dict[str, Any]]) -> None
    statuses = []  # type: List[str]
    document_id = None  # type: Optional[int]
    for card in cards:
        status = card.get("status") or ""
        if status and status not in statuses:
            statuses.append(status)
        if document_id is None and card.get("document_id"):
            document_id = card["document_id"]
    row = PhoneScan(
        created_at=datetime.utcnow(),
        filename=filename or "scan",
        file_hash=hashlib.sha256(data).hexdigest(),
        status=", ".join(statuses) or "error",
        document_id=document_id,
        payload=json.dumps({"results": cards}, ensure_ascii=False, default=str),
    )
    session.add(row)
    session.commit()


def _empty_card(status, reasons, filename):
    # type: (str, str, str) -> Dict[str, Any]
    return {
        "document_type": "",
        "entity": "",
        "nif": "",
        "date": "",
        "total": "",
        "vat": "",
        "category": "",
        "status": status,
        "confidence": 0,
        "reasons": reasons,
        "pending_item": None,
        "document_id": None,
        "filename": filename or "",
    }


def _remember_job(job):
    # type: (Dict[str, Any]) -> None
    with _JOBS_LOCK:
        _JOBS[job["id"]] = job
        if len(_JOBS) <= 40:
            return
        oldest = sorted(_JOBS.values(), key=lambda item: item["started"])
        for item in oldest[:-40]:
            _JOBS.pop(item["id"], None)


def _update_job(job_id, **changes):
    # type: (str, object) -> None
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.update(changes)


def _job_snapshot(job_id):
    # type: (str) -> Optional[Dict[str, Any]]
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return None
        return {
            "id": job["id"],
            "step": job["step"],
            "started": job["started"],
            "error": job.get("error"),
            "results": job.get("results"),
        }


def _run_job(job_id, data, filename, note):
    # type: (str, bytes, str, str) -> None
    started = time.perf_counter()
    try:
        with Session(engine) as session:
            categories = list_categories(session)
            if not categories:
                _update_job(job_id, step="error", error="not_ready")
                return

            def on_step(name):
                # type: (str) -> None
                _update_job(job_id, step=name)

            results = process_scan_bytes(
                session, data, filename, note, categories, on_step=on_step
            )
            cards = [scan_card(session, item) for item in results]
            if not cards:
                cards = [_empty_card("error", "unreadable", filename)]
            _record_scan(session, filename, data, cards)
            _update_job(job_id, step="done", results=cards, error=None)
    except Exception:
        logger.exception("scan failed")
        print("scan step error %.3fs" % (time.perf_counter() - started), flush=True)
        _update_job(job_id, step="error", error="unreadable")
        return
    seconds = time.perf_counter() - started
    logger.info("scan step total %.3fs", seconds)
    print("scan step total %.3fs" % seconds, flush=True)


def _error(code, status):
    # type: (str, int) -> JSONResponse
    return JSONResponse(status_code=status, content={"error": code})


def _strings(lang, month):
    # type: (str, str) -> str
    payload = {}
    for key in _SCAN_STRINGS:
        payload[key] = text(lang, key)
    payload["recent_href"] = page_href("/scan/recent", month, lang)
    return json.dumps(payload, ensure_ascii=False)


def render_scan_page(lang, month, months):
    # type: (str, str, List[str]) -> str
    body = """
<h1>%s</h1>
<p class="lead">%s</p>
<p><a href="%s">%s</a></p>
<form id="scan-form" action="/scan/upload" method="post" enctype="multipart/form-data">
  <label for="file">%s</label>
  <input id="file" name="file" type="file" accept="image/*,application/pdf" capture="environment" required>
  <label for="note">%s</label>
  <textarea id="note" name="note" maxlength="500"></textarea>
  <label for="code">%s</label>
  <input id="code" name="code" type="text" autocomplete="off" maxlength="40" required>
  <button id="scan-send" type="submit">%s</button>
</form>
<noscript><p class="error-card">%s</p></noscript>
<div id="result"></div>
<script type="application/json" id="scan-strings">%s</script>
""" % (
        esc(text(lang, "scan_title")),
        esc(text(lang, "scan_lead")),
        esc(page_href("/scan/recent", month, lang)),
        esc(text(lang, "see_recent")),
        esc(text(lang, "scan_file")),
        esc(text(lang, "scan_note")),
        esc(text(lang, "scan_code")),
        esc(text(lang, "scan_send")),
        esc(text(lang, "scan_noscript")),
        _strings(lang, month),
    )
    return render_page(
        lang,
        "scan",
        month,
        months,
        text(lang, "scan_title"),
        body,
        "/scan",
    )


def _stored_cards(row):
    # type: (PhoneScan) -> List[Dict[str, Any]]
    try:
        payload = json.loads(row.payload or "{}")
    except ValueError:
        return []
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return []
    return [item for item in results if isinstance(item, dict)]


def render_recent_page(lang, month, months, rows):
    # type: (str, str, List[str], List[PhoneScan]) -> str
    cards = []
    for row in rows:
        when = ""
        if row.created_at is not None:
            when = row.created_at.strftime("%Y-%m-%d %H:%M")
        stored = _stored_cards(row)
        if not stored:
            cards.append(
                "<article class='card'><h2>%s</h2><p class='quiet'>%s</p></article>"
                % (esc(row.filename), esc(when))
            )
            continue
        for card in stored:
            view = present_scan_card(card, lang)
            cards.append(
                """
<article class="card result-card">
  <p class="quiet">%s · %s</p>
  <h2 class="doc-type">%s</h2>
  <p class="entity">%s</p>
  <p class="total">%s</p>
  <p class="when">%s</p>
  <p><span class="chip chip-%s">%s</span></p>
  <p class="reason">%s</p>
  <p class="next-action"><strong>%s</strong> %s</p>
</article>
"""
                % (
                    esc(when),
                    esc(row.filename),
                    esc(view["document_type"]),
                    esc(view["entity"]),
                    esc(view["total"]),
                    esc(view["date"]),
                    esc(view["tone"]),
                    esc(view["status_label"]),
                    esc(view["reason"]),
                    esc(text(lang, "pending_next")),
                    esc(view["next_action"]),
                )
            )
    if cards:
        body_cards = "".join(cards)
    else:
        body_cards = "<p class='quiet'>%s</p>" % esc(text(lang, "scan_recent_empty"))
    body = "<h1>%s</h1><p><a href='%s'>%s</a></p>%s" % (
        esc(text(lang, "scan_recent_title")),
        esc(page_href("/scan", month, lang)),
        esc(text(lang, "scan_title")),
        body_cards,
    )
    return render_page(
        lang,
        "scan",
        month,
        months,
        text(lang, "scan_recent_title"),
        body,
        "/scan/recent",
    )


@router.get("/scan")
def scan_page(
    request: Request,
    session: Session = Depends(get_session),
):
    lang = resolve_lang(request)
    month, months = selected_month(session, request.query_params.get("month"))
    return html_response(render_scan_page(lang, month, months), lang)


@router.get("/scan/recent")
def recent_scans(
    request: Request,
    session: Session = Depends(get_session),
):
    rows = session.exec(select(PhoneScan).order_by(PhoneScan.id.desc())).all()
    latest = rows[:20]
    if not prefers_html(request):
        payload = []
        for row in latest:
            created = row.created_at.isoformat() if row.created_at else None
            payload.append(
                {
                    "id": row.id,
                    "created_at": created,
                    "filename": row.filename,
                    "status": row.status,
                    "document_id": row.document_id,
                }
            )
        return payload
    lang = resolve_lang(request)
    month, months = selected_month(session, request.query_params.get("month"))
    return html_response(render_recent_page(lang, month, months, latest), lang)


@router.post("/scan/upload")
def upload_scan(
    file: UploadFile = File(...),
    note: str = Form(""),
    code: str = Form(""),
):
    """Accept the file and return a job id. Reading continues in the background."""
    if not codes_match(code):
        return _error("bad_code", 401)
    filename = file.filename or "scan"
    if not _allowed(filename, file.content_type or ""):
        return _error("bad_type", 400)
    data = file.file.read()
    if not data:
        return _error("empty", 400)
    if len(data) > MAX_SCAN_BYTES:
        return _error("too_large", 400)
    save_scan_file(data, filename)
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "step": "uploading",
        "started": time.time(),
        "error": None,
        "results": None,
    }
    _remember_job(job)
    thread = threading.Thread(
        target=_run_job,
        args=(job_id, data, filename, note or ""),
        name="scan-%s" % job_id,
    )
    thread.daemon = True
    thread.start()
    return {"id": job_id, "step": "uploading"}


@router.get("/scan/status/{job_id}")
def scan_status(job_id: str, request: Request):
    job = _job_snapshot(job_id)
    if job is None:
        return _error("unknown_job", 404)
    lang = resolve_lang(request)
    elapsed = int(max(0, time.time() - job["started"]))
    results = None
    if job["step"] == "done":
        results = [present_scan_card(card, lang) for card in (job.get("results") or [])]
    return {
        "id": job["id"],
        "step": job["step"],
        "elapsed": elapsed,
        "error": job.get("error"),
        "results": results,
    }

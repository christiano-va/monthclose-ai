"""Phone scan page: one photo or PDF, then the same checks as an email."""
import hashlib
import hmac
import html
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from sqlmodel import Session, select

from app.db import get_session
from app.extract import list_categories
from app.models import Category, Document, PhoneScan
from app.money import to_money
from app.posting import note_value
from app.process_one import process_scan_bytes, save_scan_file
from app.settings import scan_code

logger = logging.getLogger("app.phone")
router = APIRouter()

MAX_SCAN_BYTES = 20 * 1024 * 1024


def codes_match(given):
    # type: (str) -> bool
    expected = scan_code()
    if not expected or not (given or "").strip():
        return False
    left = hashlib.sha256(given.strip().encode("utf-8")).hexdigest()
    right = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    return hmac.compare_digest(left, right)


def _wants_json(request):
    # type: (Request) -> bool
    return "application/json" in (request.headers.get("accept") or "")


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


def _esc(value):
    # type: (Any) -> str
    return html.escape("" if value is None else str(value))


def _card_html(card):
    # type: (Dict[str, Any]) -> str
    pending = card.get("pending_item")
    if pending:
        pending_html = (
            "<p class='pending'><strong>Pending item resolved:</strong> %s</p>"
            % _esc(
                "%s — %s"
                % (pending.get("id") or "", pending.get("description") or pending.get("kind") or "")
            )
        )
    else:
        pending_html = ""
    confidence = card.get("confidence")
    try:
        confidence_text = "%.2f" % float(confidence)
    except (TypeError, ValueError):
        confidence_text = _esc(confidence)
    rows = [
        ("Document type", card.get("document_type")),
        ("Entity", card.get("entity")),
        ("NIF", card.get("nif")),
        ("Date", card.get("date")),
        ("Total", card.get("total")),
        ("VAT", card.get("vat")),
        ("Category", card.get("category")),
        ("Status", card.get("status")),
        ("Confidence", confidence_text),
        ("Reasons", card.get("reasons")),
    ]
    body = []
    for label, value in rows:
        shown = value if value not in (None, "") else "—"
        if label == "Confidence":
            shown = confidence_text
        body.append(
            "<div class='row'><span>%s</span><strong>%s</strong></div>"
            % (_esc(label), _esc(shown))
        )
    return "<article class='card'>%s%s</article>" % ("".join(body), pending_html)


def render_scan_page(cards=None, error=""):
    # type: (Optional[List[Dict[str, Any]]], str) -> str
    cards = cards or []
    error_html = "<p class='error'>%s</p>" % _esc(error) if error else ""
    cards_html = "".join(_card_html(card) for card in cards)
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scan a document</title>
<style>
  body { margin: 0; padding: 16px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f4f1ea; color: #1c1917; }
  main { max-width: 32rem; margin: 0 auto; }
  h1 { font-size: 1.7rem; margin: 0 0 8px; }
  p.lead { margin: 0 0 16px; line-height: 1.4; }
  a { color: #1d4e4a; font-size: 1.1rem; }
  label { display: block; font-weight: 700; margin: 16px 0 8px; }
  input, textarea { width: 100%%; box-sizing: border-box; font-size: 1.15rem; padding: 14px; border: 1px solid #d6d3d1; border-radius: 12px; background: white; }
  textarea { min-height: 5rem; }
  button { width: 100%%; margin-top: 18px; padding: 18px; font-size: 1.25rem; font-weight: 700; color: white; background: #1d4e4a; border: 0; border-radius: 14px; }
  .card { background: white; border-radius: 16px; padding: 8px 16px 16px; margin-top: 20px; }
  .row { display: flex; justify-content: space-between; gap: 12px; padding: 10px 0; border-bottom: 1px solid #eee; }
  .row span { color: #57534e; }
  .row strong { text-align: right; }
  .pending { margin: 12px 0 0; }
  .error { color: #9f1239; font-weight: 700; }
</style>
</head>
<body>
<main>
  <h1>Scan a document</h1>
  <p class="lead">Photograph a receipt or choose a PDF. The books are updated after you send it.</p>
  <p><a href="/scan/recent">Recent scans</a></p>
  <form id="scan-form" action="/scan/upload" method="post" enctype="multipart/form-data">
    <label for="file">Document</label>
    <input id="file" name="file" type="file" accept="image/*,application/pdf" capture="environment" required>
    <label for="note">Note (optional)</label>
    <textarea id="note" name="note" maxlength="500"></textarea>
    <label for="code">Access code</label>
    <input id="code" name="code" type="text" autocomplete="off" maxlength="40" required>
    <button type="submit">Send scan</button>
  </form>
  <div id="result">%s%s</div>
</main>
<script>
document.getElementById("scan-form").addEventListener("submit", function (event) {
  if (!window.fetch) return;
  event.preventDefault();
  var form = event.target;
  var result = document.getElementById("result");
  result.innerHTML = "<p>Reading the document…</p>";
  fetch("/scan/upload", {
    method: "POST",
    body: new FormData(form),
    headers: { "Accept": "application/json" }
  }).then(function (response) {
    return response.json().then(function (body) {
      return { ok: response.ok, body: body };
    });
  }).then(function (payload) {
    var cards = (payload.body && payload.body.results) || [];
    if (!payload.ok && !cards.length) {
      var detail = (payload.body && payload.body.detail) || "The scan was not accepted.";
      result.innerHTML = "<p class='error'>" + String(detail) + "</p>";
      return;
    }
    result.innerHTML = cards.map(renderCard).join("");
  }).catch(function () {
    result.innerHTML = "<p class='error'>The scan could not be sent.</p>";
  });
});
function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"]/g, function (ch) {
    return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[ch];
  });
}
function renderCard(card) {
  var confidence = card.confidence;
  var confidenceText = (typeof confidence === "number") ? confidence.toFixed(2) : esc(confidence);
  var rows = [
    ["Document type", card.document_type],
    ["Entity", card.entity],
    ["NIF", card.nif],
    ["Date", card.date],
    ["Total", card.total],
    ["VAT", card.vat],
    ["Category", card.category],
    ["Status", card.status],
    ["Confidence", confidenceText],
    ["Reasons", card.reasons]
  ];
  var html = "<article class='card'>";
  rows.forEach(function (row) {
    var shown = row[1] === 0 || row[1] ? row[1] : "—";
    if (!shown && shown !== 0) shown = "—";
    html += "<div class='row'><span>" + esc(row[0]) + "</span><strong>" + esc(shown) + "</strong></div>";
  });
  if (card.pending_item) {
    html += "<p class='pending'><strong>Pending item resolved:</strong> " + esc(card.pending_item.description || card.pending_item.kind || "") + "</p>";
  }
  return html + "</article>";
}
</script>
</body>
</html>
""" % (
        error_html,
        cards_html,
    )


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


@router.get("/scan", response_class=HTMLResponse)
def scan_page():
    return HTMLResponse(render_scan_page())


@router.get("/scan/recent")
def recent_scans(session: Session = Depends(get_session)) -> List[dict]:
    rows = session.exec(select(PhoneScan).order_by(PhoneScan.id.desc())).all()
    payload = []
    for row in rows[:20]:
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


@router.post("/scan/upload")
def upload_scan(
    request: Request,
    file: UploadFile = File(...),
    note: str = Form(""),
    code: str = Form(""),
    session: Session = Depends(get_session),
):
    if not codes_match(code):
        raise HTTPException(status_code=401, detail="invalid access code")
    filename = file.filename or "scan"
    if not _allowed(filename, file.content_type or ""):
        raise HTTPException(status_code=400, detail="send a photo or a PDF")
    data = file.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="the file is empty")
    if len(data) > MAX_SCAN_BYTES:
        raise HTTPException(status_code=400, detail="the file is larger than 20 MB")
    save_scan_file(data, filename)
    categories = list_categories(session)
    if not categories:
        raise HTTPException(status_code=409, detail="Category table is empty")
    try:
        results = process_scan_bytes(session, data, filename, note or "", categories)
        cards = [scan_card(session, item) for item in results]
    except Exception:
        logger.exception("scan failed")
        cards = [_empty_card("error", "Could not read the scan.", filename)]
        _record_scan(session, filename, data, cards)
        if _wants_json(request):
            return JSONResponse(status_code=502, content={"results": cards})
        return HTMLResponse(render_scan_page(cards), status_code=502)
    if not cards:
        cards = [_empty_card("review", "No document was stored.", filename)]
    _record_scan(session, filename, data, cards)
    if _wants_json(request):
        return {"results": cards}
    return HTMLResponse(render_scan_page(cards))

"""File processed documents in a Drive folder this app creates.

Scope is drive.file, so the app only sees folders and files it created.
Nothing is downloaded from Drive.
"""
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from app.db import ROOT
from app.google_auth import GoogleAuthError, drive_service
from app.models import Document

logger = logging.getLogger("app.drive")

ROOT_FOLDER = "MonthClose AI"
FOLDER_MIME = "application/vnd.google-apps.folder"
_PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")
_MIME = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _slug(value, limit=60):
    # type: (Optional[str], int) -> str
    text = re.sub(r"[^A-Za-z0-9]+", "_", value or "")
    text = text.strip("_")
    if not text:
        text = "unknown"
    return text[:limit]


def is_duplicate_copy(document):
    # type: (Document) -> bool
    """A second copy (2.ª via) is not filed as a normal document."""
    return (document.status or "") == "duplicate"


def drive_filename(document):
    # type: (Document) -> str
    """Clear file name: date_entity_number.pdf (or the scan's image extension).

    A duplicate copy gets the suffix _DUPLICATE so it is not a second normal file.
    """
    if document.document_date is not None:
        stamp = document.document_date.isoformat()
    else:
        stamp = document.period or "undated"
    suffix = Path(document.original_filename or document.file_path or "").suffix.lower()
    if suffix not in _MIME:
        suffix = ".pdf"
    name = "%s_%s_%s%s" % (
        stamp,
        _slug(document.entity),
        _slug(document.document_number),
        suffix,
    )
    if not is_duplicate_copy(document):
        return name
    stem, dot, ext = name.rpartition(".")
    if dot:
        return "%s_DUPLICATE.%s" % (stem, ext)
    return name + "_DUPLICATE"


def _local_path(document):
    # type: (Document) -> Optional[Path]
    if not document.file_path:
        return None
    path = Path(document.file_path)
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        return None
    return path


def _escape(value):
    # type: (str) -> str
    return (value or "").replace("\\", "\\\\").replace("'", "\\'")


def _file_link(payload):
    # type: (Dict[str, Any]) -> Optional[str]
    link = payload.get("webViewLink")
    if link:
        return link
    file_id = payload.get("id")
    if file_id:
        return "https://drive.google.com/file/d/%s/view" % file_id
    return None


class DriveStore:
    def __init__(self, service):
        # type: (Any) -> None
        self.service = service
        self._folders = {}  # type: Dict[str, str]

    def root_folder_id(self):
        # type: () -> str
        return self._folder(ROOT_FOLDER, None)

    def period_folder_id(self, period):
        # type: (str) -> str
        name = period if _PERIOD_RE.match(period or "") else "undated"
        return self._folder(name, self.root_folder_id())

    def duplicates_folder_id(self, period):
        # type: (str) -> str
        """Second copies live under the period, in a folder of their own."""
        return self._folder("duplicates", self.period_folder_id(period))

    def folder_for(self, document):
        # type: (Document) -> str
        period = document.period or ""
        if is_duplicate_copy(document):
            return self.duplicates_folder_id(period)
        return self.period_folder_id(period)

    def _folder(self, name, parent_id):
        # type: (str, Optional[str]) -> str
        key = "%s/%s" % (parent_id or "", name)
        cached = self._folders.get(key)
        if cached:
            return cached
        found = self._find_folder(name, parent_id)
        if found is None:
            body = {"name": name, "mimeType": FOLDER_MIME}  # type: Dict[str, Any]
            if parent_id:
                body["parents"] = [parent_id]
            created = (
                self.service.files()
                .create(body=body, fields="id")
                .execute()
            )
            found = created.get("id")
        if not found:
            raise RuntimeError("drive_folder_missing")
        self._folders[key] = found
        return found

    def _find_folder(self, name, parent_id):
        # type: (str, Optional[str]) -> Optional[str]
        clauses = [
            "name = '%s'" % _escape(name),
            "mimeType = '%s'" % FOLDER_MIME,
            "trashed = false",
        ]
        if parent_id:
            clauses.append("'%s' in parents" % parent_id)
        response = (
            self.service.files()
            .list(
                q=" and ".join(clauses),
                spaces="drive",
                fields="files(id)",
                pageSize=10,
            )
            .execute()
        )
        files = response.get("files") or []
        if not files:
            return None
        return files[0].get("id")

    def existing_link(self, document_id):
        # type: (int) -> Optional[str]
        query = (
            "appProperties has { key='document_id' and value='%s' } and trashed = false"
            % int(document_id)
        )
        response = (
            self.service.files()
            .list(q=query, spaces="drive", fields="files(id, webViewLink)", pageSize=1)
            .execute()
        )
        files = response.get("files") or []
        if not files:
            return None
        return _file_link(files[0])

    def upload(self, document, path):
        # type: (Document, Path) -> Optional[str]
        from googleapiclient.http import MediaFileUpload

        folder_id = self.folder_for(document)
        suffix = path.suffix.lower()
        media = MediaFileUpload(str(path), mimetype=_MIME.get(suffix, "application/pdf"), resumable=False)
        created = (
            self.service.files()
            .create(
                body={
                    "name": drive_filename(document),
                    "parents": [folder_id],
                    "appProperties": {"document_id": str(document.id)},
                },
                media_body=media,
                fields="id, webViewLink",
            )
            .execute()
        )
        return _file_link(created)


def _empty_counts():
    # type: () -> Dict[str, int]
    return {
        "uploaded": 0,
        "already_linked": 0,
        "relinked": 0,
        "skipped_no_file": 0,
        "failed": 0,
    }


def upload_documents(session, documents):
    # type: (Session, List[Document]) -> Dict[str, int]
    """Upload documents that have a local file and no drive_link yet."""
    counts = _empty_counts()
    pending = []
    for document in documents:
        if document.drive_link:
            counts["already_linked"] += 1
            continue
        if document.id is None or not document.file_path:
            continue
        pending.append(document)
    if not pending:
        return counts
    try:
        store = DriveStore(drive_service())
    except GoogleAuthError:
        counts["failed"] = len(pending)
        return counts

    for document in pending:
        path = _local_path(document)
        if path is None:
            counts["skipped_no_file"] += 1
            continue
        try:
            link = store.existing_link(document.id)
            if link:
                document.drive_link = link
                session.add(document)
                session.commit()
                counts["relinked"] += 1
                continue
            link = store.upload(document, path)
            if not link:
                counts["failed"] += 1
                continue
            document.drive_link = link
            session.add(document)
            session.commit()
            counts["uploaded"] += 1
        except Exception as exc:
            session.rollback()
            logger.error("drive upload failed http %s", getattr(getattr(exc, "resp", None), "status", ""))
            counts["failed"] += 1
    return counts


def upload_pending(session):
    # type: (Session) -> Dict[str, int]
    rows = session.exec(select(Document)).all()
    needing = [row for row in rows if row.file_path and not row.drive_link]
    return upload_documents(session, needing)


def upload_result_documents(session, results):
    # type: (Session, List[dict]) -> Dict[str, int]
    documents = []
    seen = set()
    for item in results:
        document_id = item.get("document_id")
        if not document_id or document_id in seen:
            continue
        seen.add(document_id)
        document = session.get(Document, document_id)
        if document is not None:
            documents.append(document)
    return upload_documents(session, documents)

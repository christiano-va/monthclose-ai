"""Agenda: what the owner still has to pay or do, from every month."""
from datetime import date
from typing import Any, Dict, List, Optional

from sqlmodel import Session

from app.i18n import text
from app.present import agenda_groups, agenda_rows, render_agenda_row
from app.ui import esc, render_page


def render_agenda(session, lang, month, months, today=None):
    # type: (Session, str, str, List[str], Optional[date]) -> str
    rows = agenda_rows(session, lang, today=today)
    groups = agenda_groups(rows)
    if not rows:
        body = "<h1>%s</h1><p class='lead'>%s</p><p class='quiet'>%s</p>" % (
            esc(text(lang, "agenda_title")),
            esc(text(lang, "agenda_lead")),
            esc(text(lang, "agenda_empty")),
        )
    else:
        body = "<h1>%s</h1><p class='lead'>%s</p>%s" % (
            esc(text(lang, "agenda_title")),
            esc(text(lang, "agenda_lead")),
            "".join(
                [
                    _group(lang, "late", "agenda_overdue", groups["late"], "group-late"),
                    _group(lang, "week", "agenda_week", groups["week"], ""),
                    _group(lang, "later", "agenda_later", groups["later"], ""),
                ]
            ),
        )
    return render_page(
        lang,
        "agenda",
        month,
        months,
        text(lang, "agenda_title"),
        body,
        "/agenda",
    )


def _group(lang, key, label_key, rows, extra):
    # type: (str, str, str, List[Dict[str, Any]], str) -> str
    if not rows:
        return ""
    css = "agenda-group %s" % extra if extra else "agenda-group"
    items = "".join(render_agenda_row(row) for row in rows)
    return "<section class='%s'><h2>%s</h2><ul class='agenda-list'>%s</ul></section>" % (
        css.strip(),
        esc(text(lang, label_key)),
        items,
    )

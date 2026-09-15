"""Tables for the admin and downloadable files (CSV and Excel)."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Dict, Optional

import pandas as pd

from . import text as tx
from .models import KINDS, OPEN, RANKINGS, STATUS_LABELS, Quiz
from .scoring import Results, describe_rules

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe(value: object) -> object:
    """Neutralize spreadsheet formulas typed by participants (CSV/Excel injection)."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _sanitize(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    for column in frame.columns:
        if frame[column].dtype == object or pd.api.types.is_string_dtype(frame[column]):
            frame[column] = frame[column].map(_safe)
    return frame


def to_csv(frame: pd.DataFrame) -> bytes:
    return _sanitize(frame).to_csv(index=False).encode("utf-8-sig")


def to_excel(sheets: Dict[str, pd.DataFrame]) -> bytes:
    from openpyxl.styles import Font

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            title = name[:31]
            _sanitize(frame).to_excel(writer, sheet_name=title, index=False)
            sheet = writer.sheets[title]
            sheet.freeze_panes = "A2"
            for cell in sheet[1]:
                cell.font = Font(bold=True)
            for column_cells in sheet.columns:
                width = max((len(str(cell.value)) for cell in column_cells if cell.value is not None), default=8)
                sheet.column_dimensions[column_cells[0].column_letter].width = min(max(10, width + 2), 60)
    return buffer.getvalue()


def local_time(timestamp: Optional[float], timezone_name: Optional[str]) -> Optional[datetime]:
    """A naive local datetime (Excel has no time zones) or None."""
    if timestamp is None:
        return None
    moment = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    if timezone_name:
        try:
            from zoneinfo import ZoneInfo

            moment = moment.astimezone(ZoneInfo(timezone_name))
        except Exception:  # noqa: BLE001 - unknown zone or missing tz database
            pass
    return moment.replace(tzinfo=None, microsecond=0)


def ranking_frame(results: Results, timezone_name: Optional[str] = None, include_email: bool = True) -> pd.DataFrame:
    rows = []
    for row in results.rows:
        item = {
            "Rank": row.rank,
            "Winner": "🏆" if row.winner else "",
            "Name": row.name,
            "Email": row.email,
            "Points": row.points,
            "Score %": row.percent,
            "Right answers": row.right,
            "Answered": f"{row.seen}/{row.total_questions}",
            "Time (s)": row.seconds,
            "Finished": row.finished,
            "Can win": row.eligible,
            "Can't win because": row.reason,
            "Joined at": local_time(row.joined_at, timezone_name),
            "Finished at": local_time(row.finished_at, timezone_name),
        }
        if not include_email:
            item.pop("Email")
        rows.append(item)
    columns = ["Rank", "Winner", "Name", "Email", "Points", "Score %", "Right answers", "Answered", "Time (s)",
               "Finished", "Can win", "Can't win because", "Joined at", "Finished at"]
    if not include_email:
        columns.remove("Email")
    return pd.DataFrame(rows, columns=columns)


def answers_frame(quiz: Quiz, results: Results, timezone_name: Optional[str] = None) -> pd.DataFrame:
    """One row per participant and four columns per question: answer, right, points and seconds."""
    rows = []
    for row in results.rows:
        participant = quiz.participants.get(row.token)
        item: Dict[str, object] = {"Rank": row.rank, "Name": row.name, "Email": row.email, "Points": row.points,
                                   "Time (s)": row.seconds}
        for number, question in enumerate(quiz.questions, start=1):
            answer = participant.answers.get(question.id) if participant else None
            grade = row.grades.get(question.id)
            if answer is None:
                shown = "(not reached)"
            elif answer.skipped:
                shown = "(skipped)"
            elif question.kind == OPEN:
                shown = answer.text
            else:
                shown = "; ".join(question.option_texts(answer.choice))
            item[f"Q{number} answer"] = shown
            item[f"Q{number} right"] = "" if grade is None or grade.right is None else ("yes" if grade.right else "no")
            item[f"Q{number} points"] = round(grade.points, 1) if grade and question.scored else None
            item[f"Q{number} seconds"] = round(answer.seconds, 1) if answer else None
        rows.append(item)
    return pd.DataFrame(rows)


def questions_frame(results: Results) -> pd.DataFrame:
    rows = []
    for stats in results.questions:
        question = stats.question
        if question.is_choice:
            options = "; ".join(f"{o.text} ({o.count})" for o in stats.options)
            right = "; ".join(o.text for o in stats.options if o.right)
        else:
            options = "; ".join(f"{g.text} ({g.count})" for g in stats.open_groups[:30])
            right = "; ".join(question.accepted)
        rows.append({
            "No.": stats.number,
            "Question": question.text,
            "Type": KINDS[question.kind],
            "Required": question.required,
            "Points": question.points,
            "Right answers": right if question.scored else "(not scored)",
            "Answers given (people)": options,
            "Reached": stats.seen,
            "Answered": stats.answered,
            "Right": stats.right if question.scored else None,
            "Right %": round(stats.percent_right, 1) if stats.percent_right is not None else None,
            "Average time (s)": round(stats.average_seconds, 1) if stats.average_seconds is not None else None,
        })
    return pd.DataFrame(rows)


def summary_frame(quiz: Quiz, results: Results, now: float, timezone_name: Optional[str] = None) -> pd.DataFrame:
    settings = quiz.settings
    values = [
        ("Quiz", quiz.title),
        ("Code", tx.format_code(quiz.code)),
        ("Status", STATUS_LABELS[quiz.status(now)]),
        ("Opened at", local_time(quiz.opened_at, timezone_name)),
        ("Closed at", local_time(quiz.closed_at or (quiz.closes_at if quiz.status(now) == "closed" else None),
                                 timezone_name)),
        ("Questions", len(quiz.questions)),
        ("Maximum points", results.max_points),
        ("Participants", results.participants),
        ("Finished", results.finished),
        ("Average score %", round(results.average_percent, 1) if results.average_percent is not None else None),
        ("Ranking", RANKINGS[settings.ranking]),
        ("Winning rules", "; ".join(describe_rules(settings, results.max_points))),
        ("Winners", "; ".join(f"{w.rank}. {w.name}" for w in results.winners)),
        ("Exported at", local_time(now, timezone_name)),
        ("Time zone", timezone_name or "UTC"),
    ]
    return pd.DataFrame(values, columns=["Field", "Value"]).astype({"Value": object})


def results_workbook(quiz: Quiz, results: Results, now: float, timezone_name: Optional[str] = None) -> bytes:
    return to_excel({
        "Summary": summary_frame(quiz, results, now, timezone_name),
        "Ranking": ranking_frame(results, timezone_name),
        "Answers": answers_frame(quiz, results, timezone_name),
        "Questions": questions_frame(results),
    })

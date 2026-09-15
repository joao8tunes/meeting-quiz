"""Quiz files: questions and settings saved as JSON, to recreate a quiz after the app forgets it.

Quiz files hold the right answers but never responses, participants or the admin key.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, fields
from typing import Any, Dict, List, Tuple

from .models import (
    MAX_ACCEPTED, MAX_OPTIONS, MAX_QUESTIONS, OPEN, SINGLE, KINDS, Option, Question, Quiz, Settings, new_id,
)

FILE_KIND = "meeting-quiz"
MAX_FILE_BYTES = 512 * 1024


class QuizFileError(ValueError):
    pass


def dump(quiz: Quiz) -> bytes:
    questions = []
    for question in quiz.questions:
        item: Dict[str, Any] = {"type": question.kind, "text": question.text, "required": question.required,
                                "points": question.points}
        if question.is_choice:
            item["options"] = [{"text": o.text, "right": o.id in question.correct} for o in question.options]
            if question.kind != SINGLE:
                item["partial_credit"] = question.partial_credit
        else:
            item["accepted_answers"] = list(question.accepted)
            item["accept_typos"] = question.tolerant
        questions.append(item)
    payload = {"kind": FILE_KIND, "title": quiz.title, "description": quiz.description,
               "settings": asdict(quiz.settings), "questions": questions}
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def load(data: bytes) -> Tuple[str, str, List[Question], Settings]:
    """Read a quiz file. Values are only shaped here; the store cleans and limits them again."""
    if len(data) > MAX_FILE_BYTES:
        raise QuizFileError("This file is too big to be a quiz file.")
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise QuizFileError("This isn't a quiz file (it isn't valid JSON).") from None
    if not isinstance(payload, dict) or payload.get("kind") != FILE_KIND:
        raise QuizFileError("This isn't a quiz file saved by this app.")
    raw_questions = payload.get("questions")
    if not isinstance(raw_questions, list):
        raise QuizFileError("The quiz file has no questions list.")
    questions = [_question(item, number) for number, item in enumerate(raw_questions[:MAX_QUESTIONS], start=1)]
    return _text(payload.get("title")), _text(payload.get("description")), questions, _settings(payload.get("settings"))


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _question(item: Any, number: int) -> Question:
    if not isinstance(item, dict):
        raise QuizFileError(f"Question {number} is not valid.")
    kind = item.get("type")
    if kind not in KINDS:
        raise QuizFileError(f"Question {number} has an unknown type: {str(kind)[:30]!r}.")
    question = Question(id=new_id(), kind=kind, text=_text(item.get("text")),
                        required=item.get("required", True) is not False, points=_int(item.get("points"), 100),
                        partial_credit=item.get("partial_credit") is True, tolerant=item.get("accept_typos") is not False)
    if kind == OPEN:
        accepted = item.get("accepted_answers") or []
        question.accepted = [value for value in accepted if isinstance(value, str)][:MAX_ACCEPTED] \
            if isinstance(accepted, list) else []
    else:
        options = item.get("options") or []
        if not isinstance(options, list):
            raise QuizFileError(f"Question {number} has invalid options.")
        for option in options[:MAX_OPTIONS]:
            text, right = (option.get("text"), option.get("right") is True) if isinstance(option, dict) \
                else (option, False)
            question.options.append(Option(new_id(), _text(text)))
            if right:
                question.correct.append(question.options[-1].id)
    return question


def _settings(value: Any) -> Settings:
    settings = Settings()
    if not isinstance(value, dict):
        return settings
    for item in fields(Settings):
        if item.name not in value:
            continue
        default, given = getattr(settings, item.name), value[item.name]
        if isinstance(default, bool):
            if isinstance(given, bool):
                setattr(settings, item.name, given)
        elif isinstance(default, int):
            setattr(settings, item.name, _int(given, default))
        elif isinstance(default, str):
            if isinstance(given, str):
                setattr(settings, item.name, given)
        elif isinstance(default, list) and isinstance(given, list):
            setattr(settings, item.name, [entry for entry in given if isinstance(entry, str)][:500])
    return settings


def _int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return int(value)

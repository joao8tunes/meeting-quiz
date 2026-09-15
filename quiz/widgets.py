"""Self-contained HTML widgets: the countdown and the winners reveal.

Text typed by people reaches the page as escaped JSON and is rendered with ``textContent``, never as HTML.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Optional

from . import text as tx
from .models import Quiz
from .scoring import Results, describe_rules

COUNTDOWN_HEIGHT = 46
COUNTDOWN_LARGE_HEIGHT = 112


@lru_cache(maxsize=None)
def _template(name: str) -> str:
    return Path(__file__).with_name(name).read_text(encoding="utf-8")


def _json_for_script(payload: dict) -> str:
    text = json.dumps(payload, ensure_ascii=True)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def countdown_html(remaining: float, total: Optional[float], large: bool = False, dark: bool = False) -> str:
    payload = {"remaining": max(0.0, remaining), "total": total, "large": large, "dark": dark,
               "label": "Time left", "doneLabel": "Time's up"}
    return _template("countdown.html").replace("__PAYLOAD__", _json_for_script(payload))


def winners_height(count: int) -> int:
    extra_rows = math.ceil(max(0, count - 3) / 4)
    return 470 + min(3, extra_rows) * 52


def winners_html(quiz: Quiz, results: Results, reveal_all: bool = False) -> str:
    winners = [
        {"place": index, "name": row.name, "points": f"{row.points:g} pts",
         "detail": f"{row.percent:g}% · {tx.format_seconds(row.seconds)}"}
        for index, row in enumerate(results.winners, start=1)
    ]
    rules = describe_rules(quiz.settings, quiz.max_points)
    payload = {
        "eyebrow": "🏆 Winners",
        "title": quiz.title,
        "subtitle": f"{tx.plural(results.participants, 'participant')} · {' · '.join(rules)}",
        "winners": winners,
        "emptyText": "No one meets the winning rules yet.",
        "doneText": "🎉 Congratulations to the winners!",
        "revealAll": reveal_all,
    }
    return _template("winners.html").replace("__PAYLOAD__", _json_for_script(payload))

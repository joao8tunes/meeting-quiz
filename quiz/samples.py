"""A ready-made quiz that shows every question type."""

from __future__ import annotations

from typing import List, Tuple

from .models import MULTIPLE, OPEN, SINGLE, Option, Question, Settings, new_id

SAMPLE_TITLE = "Sample quiz: general knowledge"
SAMPLE_DESCRIPTION = "Seven quick questions to see how the quiz works. Answer fast: time breaks ties!"


def _choice(kind: str, text: str, options: List[Tuple[str, bool]], **extra) -> Question:
    question = Question(id=new_id(), kind=kind, text=text, **extra)
    for label, right in options:
        question.options.append(Option(new_id(), label))
        if right:
            question.correct.append(question.options[-1].id)
    return question


def sample_quiz() -> Tuple[str, str, List[Question], Settings]:
    questions = [
        _choice(SINGLE, "How many minutes are there in a day?",
                [("1,440", True), ("1,240", False), ("2,400", False), ("960", False)]),
        _choice(MULTIPLE, "Which of these are programming languages?",
                [("Python", True), ("Rust", True), ("JPEG", False), ("Kotlin", True), ("USB", False)],
                partial_credit=True),
        _choice(SINGLE, "Which HTTP status code means “Not Found”?",
                [("200", False), ("301", False), ("404", True), ("500", False)]),
        Question(id=new_id(), kind=OPEN, text="Which planet is known as the Red Planet?", accepted=["Mars"]),
        _choice(MULTIPLE, "Select the prime numbers.",
                [("2", True), ("4", False), ("7", True), ("9", False), ("11", True)], partial_credit=True),
        _choice(SINGLE, "What does the “www” in a web address stand for?",
                [("World Wide Web", True), ("Web World Wide", False), ("Wide Web World", False)]),
        Question(id=new_id(), kind=OPEN, text="What topic would you like to see in the next session?",
                 required=False, points=0),
    ]
    return SAMPLE_TITLE, SAMPLE_DESCRIPTION, questions, Settings(time_limit_minutes=5, winners=3)

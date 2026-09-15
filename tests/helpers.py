"""Shared test helpers: a store with a controllable clock and small quizzes."""

from __future__ import annotations

from typing import List, Tuple

from quiz.models import MULTIPLE, OPEN, SINGLE, Option, Question, Settings, new_id
from quiz.store import QuizStore


class Clock:
    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def choice(kind: str, text: str, options: List[Tuple[str, bool]], **extra) -> Question:
    question = Question(id=new_id(), kind=kind, text=text, **extra)
    for label, right in options:
        question.options.append(Option(new_id(), label))
        if right:
            question.correct.append(question.options[-1].id)
    return question


def small_quiz() -> List[Question]:
    return [
        choice(SINGLE, "2 + 2?", [("3", False), ("4", True), ("5", False)]),
        choice(MULTIPLE, "Even numbers?", [("2", True), ("3", False), ("4", True)], partial_credit=True),
        Question(id=new_id(), kind=OPEN, text="Red planet?", accepted=["Mars"]),
        Question(id=new_id(), kind=OPEN, text="Feedback?", required=False, points=0),
    ]


def make_store(**settings) -> Tuple[QuizStore, Clock, str, str]:
    clock = Clock()
    store = QuizStore(clock=clock)
    quiz, key = store.create("Test quiz", "", small_quiz(), Settings(**settings))
    return store, clock, quiz.code, key


def answer_all(store: QuizStore, clock: Clock, code: str, token: str, right: bool = True, seconds: float = 2.0) -> None:
    """Answer every question of the small quiz (right or wrong), ``seconds`` apart."""
    quiz = store.get(code)
    for question in quiz.questions:
        store.show(code, token, question.id)
        clock.advance(seconds)
        if question.kind == OPEN:
            if question.required:
                store.answer(code, token, question.id, text="mars" if right else "venus")
            else:
                store.answer(code, token, question.id, skipped=True)
        else:
            picked = question.correct if right else [o.id for o in question.options if o.id not in question.correct]
            store.answer(code, token, question.id, choice=picked[:1] if question.kind == SINGLE else picked)

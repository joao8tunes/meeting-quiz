"""Quiz data: questions, settings, participants and their answers."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Dict, List, Optional

SINGLE, MULTIPLE, OPEN = "single", "multiple", "open"
KINDS: Dict[str, str] = {
    SINGLE: "Single choice",
    MULTIPLE: "Multiple choice",
    OPEN: "Open answer",
}
KIND_HELP: Dict[str, str] = {
    SINGLE: "People pick one option.",
    MULTIPLE: "People pick every option that applies.",
    OPEN: "People type their answer.",
}

POINTS_THEN_TIME, SPEED_BONUS = "points_then_time", "speed_bonus"
RANKINGS: Dict[str, str] = {
    POINTS_THEN_TIME: "Most points, then fastest",
    SPEED_BONUS: "Points with a speed bonus",
}
RANKING_HELP: Dict[str, str] = {
    POINTS_THEN_TIME: "Right answers decide the ranking; when points are tied, whoever took less time ranks higher.",
    SPEED_BONUS: "Each right answer earns up to 50% extra points when answered quickly. The bonus shrinks to zero "
                 "over the bonus window. Ties are still broken by time.",
}
SPEED_BONUS_SHARE = 0.5

DRAFT, OPEN_STATUS, CLOSED = "draft", "open", "closed"
STATUS_LABELS = {DRAFT: "Draft", OPEN_STATUS: "Open", CLOSED: "Closed"}

# Limits keep one public instance healthy: everything lives in memory.
MAX_TITLE = 120
MAX_DESCRIPTION = 1000
MAX_QUESTION = 500
MAX_OPTION = 200
MAX_OPTIONS = 10
MIN_OPTIONS = 2
MAX_QUESTIONS = 50
MAX_ACCEPTED = 20
MAX_ANSWER = 500
MAX_NAME = 80
MAX_EMAIL = 254
MAX_POINTS = 1000
MAX_WINNERS = 50
MAX_DURATION_MINUTES = 24 * 60
MAX_NOT_ELIGIBLE = 200


def new_id() -> str:
    return secrets.token_hex(4)


@dataclass
class Option:
    id: str
    text: str = ""


@dataclass
class Question:
    id: str
    kind: str = SINGLE
    text: str = ""
    options: List[Option] = field(default_factory=list)
    correct: List[str] = field(default_factory=list)       # ids of the right options
    accepted: List[str] = field(default_factory=list)      # open answers counted as right
    required: bool = True
    points: int = 100
    partial_credit: bool = False                           # multiple choice: credit for each right pick
    tolerant: bool = True                                  # open answers: accept small typos
    reviews: Dict[str, bool] = field(default_factory=dict)  # normalized open answer -> right? (admin review)

    @property
    def scored(self) -> bool:
        return self.points > 0

    @property
    def is_choice(self) -> bool:
        return self.kind in (SINGLE, MULTIPLE)

    def option(self, option_id: str) -> Optional[Option]:
        return next((option for option in self.options if option.id == option_id), None)

    def option_texts(self, option_ids: List[str]) -> List[str]:
        texts = [self.option(option_id) for option_id in option_ids]
        return [option.text for option in texts if option is not None]

    def issues(self) -> List[str]:
        """What must be fixed before people can answer this question."""
        problems = []
        if not self.text.strip():
            problems.append("has no text")
        if self.is_choice:
            filled = [option for option in self.options if option.text.strip()]
            if len(filled) < MIN_OPTIONS:
                problems.append(f"needs at least {MIN_OPTIONS} options")
            if len(filled) != len(self.options):
                problems.append("has empty options")
            if self.scored:
                right = [option_id for option_id in self.correct if self.option(option_id)]
                if not right:
                    problems.append("has no right answer marked (or set its points to 0 to make it a poll)")
                elif self.kind == SINGLE and len(right) > 1:
                    problems.append("is single choice but has more than one right answer")
        return problems

    def notes(self) -> List[str]:
        """Things worth knowing that don't block the quiz."""
        if self.kind == OPEN and self.scored and not self.accepted:
            return ["has no accepted answers: review the answers in Results to give points"]
        return []


@dataclass
class Settings:
    time_limit_minutes: int = 10          # 0 = open until the admin closes it
    ask_email: bool = True
    email_domains: List[str] = field(default_factory=list)
    shuffle_options: bool = True
    ranking: str = POINTS_THEN_TIME
    bonus_window_seconds: int = 20
    winners: int = 3
    min_score_percent: int = 0
    require_finished: bool = True
    not_eligible: List[str] = field(default_factory=list)   # emails that can't win (e.g. organizers)


@dataclass
class Answer:
    question_id: str
    shown_at: float
    answered_at: float
    choice: List[str] = field(default_factory=list)   # option ids
    text: str = ""
    skipped: bool = False

    @property
    def seconds(self) -> float:
        return max(0.0, self.answered_at - self.shown_at)


@dataclass
class Participant:
    token: str
    name: str
    email: str
    joined_at: float
    shown_at: Dict[str, float] = field(default_factory=dict)
    answers: Dict[str, Answer] = field(default_factory=dict)
    finished_at: Optional[float] = None

    @property
    def last_activity(self) -> float:
        times = [self.joined_at] + [answer.answered_at for answer in self.answers.values()]
        return max(times)


@dataclass
class Quiz:
    code: str
    admin_salt: bytes
    admin_hash: bytes
    created_at: float
    updated_at: float
    title: str = "Untitled quiz"
    description: str = ""
    questions: List[Question] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)
    opened_at: Optional[float] = None
    closes_at: Optional[float] = None   # None while open = no time limit
    closed_at: Optional[float] = None
    participants: Dict[str, Participant] = field(default_factory=dict)
    emails: Dict[str, str] = field(default_factory=dict)   # email -> participant token
    revision: int = 0          # any change
    layout_revision: int = 0   # questions added, removed, moved or replaced

    def status(self, now: float) -> str:
        if self.opened_at is None:
            return DRAFT
        if self.closed_at is not None or (self.closes_at is not None and now >= self.closes_at):
            return CLOSED
        return OPEN_STATUS

    def remaining(self, now: float) -> Optional[float]:
        if self.status(now) != OPEN_STATUS or self.closes_at is None:
            return None
        return max(0.0, self.closes_at - now)

    def question(self, question_id: str) -> Optional[Question]:
        return next((question for question in self.questions if question.id == question_id), None)

    def issues(self) -> List[str]:
        problems = [] if self.questions else ["Add at least one question."]
        for number, question in enumerate(self.questions, start=1):
            problems.extend(f"Question {number} {issue}." for issue in question.issues())
        return problems

    def notes(self) -> List[str]:
        return [f"Question {number} {note}." for number, question in enumerate(self.questions, start=1)
                for note in question.notes()]

    @property
    def max_points(self) -> int:
        return sum(question.points for question in self.questions if question.scored)

    @property
    def locked(self) -> bool:
        """Once someone joined, questions and options can't change (answer keys and points still can)."""
        return bool(self.participants)

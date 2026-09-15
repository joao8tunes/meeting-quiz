"""Grading answers, ranking participants and picking winners."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from . import text as tx
from .models import MULTIPLE, OPEN, SPEED_BONUS, SPEED_BONUS_SHARE, Answer, Participant, Question, Quiz, Settings


@dataclass
class Grade:
    right: Optional[bool]   # None: the question isn't scored (a poll or feedback)
    fraction: float         # share of the question's points earned before any bonus
    points: float
    bonus: float = 0.0


@dataclass
class Row:
    rank: int
    token: str
    name: str
    email: str
    points: float
    max_points: int
    percent: float
    right: int
    answered: int
    seen: int
    total_questions: int
    seconds: float
    finished: bool
    joined_at: float
    finished_at: Optional[float]
    eligible: bool = True
    reason: str = ""
    winner: bool = False
    grades: Dict[str, Grade] = field(default_factory=dict)


@dataclass
class OptionCount:
    option_id: str
    text: str
    count: int
    right: bool


@dataclass
class OpenGroup:
    key: str          # normalized answer
    text: str         # the most common way people typed it
    count: int
    automatic: bool   # matches an accepted answer
    review: Optional[bool]

    @property
    def right(self) -> bool:
        return self.automatic if self.review is None else self.review


@dataclass
class QuestionStats:
    number: int
    question: Question
    seen: int
    answered: int
    right: int
    average_seconds: Optional[float]
    options: List[OptionCount] = field(default_factory=list)
    open_groups: List[OpenGroup] = field(default_factory=list)

    @property
    def percent_right(self) -> Optional[float]:
        if not self.question.scored or not self.seen:
            return None
        return 100.0 * self.right / self.seen


@dataclass
class Results:
    rows: List[Row]
    winners: List[Row]
    questions: List[QuestionStats]
    max_points: int

    @property
    def participants(self) -> int:
        return len(self.rows)

    @property
    def finished(self) -> int:
        return sum(row.finished for row in self.rows)

    @property
    def average_percent(self) -> Optional[float]:
        return sum(row.percent for row in self.rows) / len(self.rows) if self.rows and self.max_points else None


@lru_cache(maxsize=50_000)
def _open_match(answer: str, accepted: Tuple[str, ...], tolerant: bool) -> bool:
    return tx.matches(answer, accepted, tolerant)


def open_answer_automatic(question: Question, answer_text: str) -> bool:
    return _open_match(tx.normalize(answer_text), tuple(question.accepted), question.tolerant)


def open_answer_right(question: Question, answer_text: str) -> bool:
    review = question.reviews.get(tx.normalize(answer_text))
    return open_answer_automatic(question, answer_text) if review is None else review


def _fraction(question: Question, answer: Answer) -> float:
    if question.kind == OPEN:
        return 1.0 if open_answer_right(question, answer.text) else 0.0
    right = {option_id for option_id in question.correct if question.option(option_id)}
    if not right:
        return 0.0
    picked = set(answer.choice)
    if picked == right:
        return 1.0
    if question.kind == MULTIPLE and question.partial_credit:
        return max(0.0, (len(picked & right) - len(picked - right)) / len(right))
    return 0.0


def grade(question: Question, answer: Optional[Answer], settings: Settings) -> Grade:
    if not question.scored:
        return Grade(None, 0.0, 0.0)
    if answer is None or answer.skipped:
        return Grade(False, 0.0, 0.0)
    fraction = _fraction(question, answer)
    bonus = 0.0
    if settings.ranking == SPEED_BONUS and fraction > 0:
        speed = max(0.0, 1.0 - answer.seconds / max(1, settings.bonus_window_seconds))
        bonus = question.points * fraction * SPEED_BONUS_SHARE * speed
    return Grade(fraction >= 1.0, fraction, question.points * fraction + bonus, bonus)


def _row(quiz: Quiz, participant: Participant) -> Row:
    grades = {q.id: grade(q, participant.answers.get(q.id), quiz.settings) for q in quiz.questions}
    answers = [participant.answers[q.id] for q in quiz.questions if q.id in participant.answers]
    points = round(sum(g.points for g in grades.values()), 1)
    max_points = quiz.max_points
    return Row(
        rank=0, token=participant.token, name=participant.name, email=participant.email, points=points,
        max_points=max_points, percent=round(100.0 * min(points, max_points) / max_points, 1) if max_points else 0.0,
        right=sum(1 for g in grades.values() if g.right), answered=sum(1 for a in answers if not a.skipped),
        seen=len(answers), total_questions=len(quiz.questions), seconds=round(sum(a.seconds for a in answers), 1),
        finished=participant.finished_at is not None, joined_at=participant.joined_at,
        finished_at=participant.finished_at, grades=grades,
    )


def rank(quiz: Quiz) -> Results:
    rows = [_row(quiz, participant) for participant in quiz.participants.values()]
    rows.sort(key=lambda r: (-r.points, r.seconds, r.finished_at if r.finished_at is not None else float("inf"),
                             r.joined_at))
    settings = quiz.settings
    blocked = set(settings.not_eligible)
    winners: List[Row] = []
    for position, row in enumerate(rows, start=1):
        row.rank = position
        if row.email and row.email in blocked:
            row.eligible, row.reason = False, "On the not eligible list"
        elif settings.require_finished and not row.finished:
            row.eligible, row.reason = False, "Didn't finish"
        elif quiz.max_points and row.points <= 0:
            row.eligible, row.reason = False, "No points"
        elif row.percent < settings.min_score_percent:
            row.eligible, row.reason = False, f"Below {settings.min_score_percent}%"
        if row.eligible and len(winners) < settings.winners:
            row.winner = True
            winners.append(row)
    return Results(rows=rows, winners=winners, questions=question_stats(quiz, rows), max_points=quiz.max_points)


def question_stats(quiz: Quiz, rows: List[Row]) -> List[QuestionStats]:
    by_token = {row.token: row for row in rows}
    stats = []
    for number, question in enumerate(quiz.questions, start=1):
        answers = [p.answers[question.id] for p in quiz.participants.values() if question.id in p.answers]
        given = [a for a in answers if not a.skipped]
        right = sum(1 for p in quiz.participants.values()
                    if question.id in p.answers and by_token[p.token].grades[question.id].right)
        item = QuestionStats(
            number=number, question=question, seen=len(answers), answered=len(given), right=right,
            average_seconds=sum(a.seconds for a in answers) / len(answers) if answers else None,
        )
        if question.is_choice:
            counts = Counter(option_id for a in given for option_id in a.choice)
            item.options = [OptionCount(o.id, o.text, counts.get(o.id, 0), o.id in question.correct)
                            for o in question.options]
        else:
            spellings: Dict[str, Counter] = defaultdict(Counter)
            for a in given:
                key = tx.normalize(a.text)
                if key:
                    spellings[key][a.text] += 1
            groups = [OpenGroup(key=key, text=forms.most_common(1)[0][0], count=sum(forms.values()),
                                automatic=open_answer_automatic(question, key), review=question.reviews.get(key))
                      for key, forms in spellings.items()]
            item.open_groups = sorted(groups, key=lambda g: (-g.count, g.key))
        stats.append(item)
    return stats


def progress(quiz: Quiz, participant: Participant) -> str:
    if participant.finished_at is not None:
        return "Finished"
    return f"{len(participant.answers)}/{len(quiz.questions)}" if participant.answers else "Joined"


def describe_rules(settings: Settings, max_points: int) -> List[str]:
    """Plain-language winning rules, shown to the admin and on the winners screen."""
    rules = [tx.plural(settings.winners, "winner")]
    rules.append("most points, fastest breaks ties" if settings.ranking != SPEED_BONUS
                 else f"points plus speed bonus ({settings.bonus_window_seconds} s window)")
    if settings.require_finished:
        rules.append("finished the quiz")
    if settings.min_score_percent:
        rules.append(f"at least {settings.min_score_percent}% of the points")
    if max_points:
        rules.append("at least one point")
    if settings.not_eligible:
        rules.append(f"{tx.plural(len(settings.not_eligible), 'email')} not eligible")
    return rules

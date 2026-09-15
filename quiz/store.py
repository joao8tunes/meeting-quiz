"""In-memory quiz storage shared by every session of the app.

Nothing is written to disk: quizzes disappear after ``ttl_hours`` without activity or whenever the app
process restarts. Every change goes through this class, under one lock, and every rule that protects a
quiz (who can join, what can still be edited, when answers are accepted) is enforced here, not in the UI.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import replace
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from . import text as tx
from .models import (
    DRAFT, MAX_ACCEPTED, MAX_ANSWER, MAX_DESCRIPTION, MAX_DURATION_MINUTES, MAX_NAME,
    MAX_NOT_ELIGIBLE, MAX_OPTION, MAX_OPTIONS, MAX_POINTS, MAX_QUESTION, MAX_QUESTIONS, MAX_TITLE, MAX_WINNERS,
    MULTIPLE, OPEN, OPEN_STATUS, RANKINGS, SINGLE, Answer, Option, Participant, Question, Quiz, Settings, new_id,
)

TTL_HOURS = 48
MAX_QUIZZES = 500
MAX_PARTICIPANTS = 3000          # per quiz
MAX_TOTAL_PARTICIPANTS = 20000   # across the whole app
GRACE_SECONDS = 2.0              # answers sent right as time runs out still count
SWEEP_EVERY = 60.0

ADMIN_KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O or 1/I look-alikes
ADMIN_KEY_LENGTH = 12                                    # 32^12 = 60 bits
_HASH_ITERATIONS = 60_000


class StoreError(Exception):
    """A rule was broken; the message is meant for people."""


def new_admin_key() -> str:
    raw = "".join(secrets.choice(ADMIN_KEY_ALPHABET) for _ in range(ADMIN_KEY_LENGTH))
    return "-".join(raw[i:i + 4] for i in range(0, ADMIN_KEY_LENGTH, 4))


def normalize_admin_key(value: object) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _hash_key(key: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", normalize_admin_key(key).encode(), salt, _HASH_ITERATIONS)


def default_question(kind: str) -> Question:
    question = Question(id=new_id(), kind=kind)
    if kind in (SINGLE, MULTIPLE):
        question.options = [Option(new_id()), Option(new_id())]
    return question


class QuizStore:
    def __init__(self, clock: Callable[[], float] = time.time, ttl_hours: float = TTL_HOURS,
                 max_quizzes: int = MAX_QUIZZES, max_participants: int = MAX_PARTICIPANTS,
                 max_total_participants: int = MAX_TOTAL_PARTICIPANTS):
        self._clock = clock
        self._ttl = ttl_hours * 3600
        self._max_quizzes = max_quizzes
        self._max_participants = max_participants
        self._max_total_participants = max_total_participants
        self._lock = threading.RLock()
        self._quizzes: Dict[str, Quiz] = {}
        self._used_codes: set = set()
        self._last_sweep = 0.0

    @property
    def ttl_hours(self) -> float:
        return self._ttl / 3600

    def now(self) -> float:
        return self._clock()

    # ------------------------------------------------------------------ quizzes

    def create(self, title: str = "", description: str = "", questions: Iterable[Question] = (),
               settings: Optional[Settings] = None) -> Tuple[Quiz, str]:
        """Create a quiz and return it with its admin key (shown once, only its hash is kept)."""
        key, salt = new_admin_key(), secrets.token_bytes(16)
        digest = _hash_key(key, salt)   # slow on purpose: keep it outside the lock
        with self._lock:
            self._sweep(force=True)
            if len(self._quizzes) >= self._max_quizzes:
                raise StoreError("The app is holding too many quizzes right now. Please try again later.")
            code = self._new_code()
            now = self.now()
            quiz = Quiz(code=code, admin_salt=salt, admin_hash=digest, created_at=now, updated_at=now,
                        title=tx.clean(title, MAX_TITLE) or "Untitled quiz",
                        description=tx.clean(description, MAX_DESCRIPTION, multiline=True))
            quiz.questions = [self._sanitize_question(question) for question in list(questions)[:MAX_QUESTIONS]]
            quiz.settings = self._sanitize_settings(settings or Settings())
            self._quizzes[code] = quiz
            return self._copy(quiz), key

    def get(self, code: object) -> Optional[Quiz]:
        """A consistent read-only copy of the quiz (safe to read while people keep answering)."""
        with self._lock:
            self._sweep()
            quiz = self._quizzes.get(tx.only_digits(code))
            return self._copy(quiz) if quiz else None

    def authenticate(self, code: object, key: object) -> Optional[Quiz]:
        code = tx.only_digits(code)
        with self._lock:
            quiz = self._quizzes.get(code)
            salt, expected = (quiz.admin_salt, quiz.admin_hash) if quiz else (b"\0" * 16, b"")
        digest = _hash_key(str(key or ""), salt)   # same work whether or not the quiz exists, outside the lock
        if not expected or not hmac.compare_digest(digest, expected):
            return None
        with self._lock:
            quiz = self._quizzes.get(code)
            if quiz is None or quiz.admin_hash != expected:
                return None
            self._touch(quiz, changed=False)
            return self._copy(quiz)

    def touch(self, code: str) -> None:
        with self._lock:
            quiz = self._quizzes.get(code)
            if quiz:
                self._touch(quiz, changed=False)

    def delete(self, code: str) -> None:
        with self._lock:
            self._quizzes.pop(code, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._quizzes)

    # ------------------------------------------------------------------ editing

    def update_details(self, code: str, title: object = None, description: object = None) -> None:
        with self._edit(code) as quiz:
            if title is not None:
                quiz.title = tx.clean(title, MAX_TITLE) or "Untitled quiz"
            if description is not None:
                quiz.description = tx.clean(description, MAX_DESCRIPTION, multiline=True)

    def add_question(self, code: str, kind: str) -> str:
        with self._edit(code, frozen=True, layout=True) as quiz:
            if len(quiz.questions) >= MAX_QUESTIONS:
                raise StoreError(f"A quiz can have up to {MAX_QUESTIONS} questions.")
            question = default_question(kind if kind in (SINGLE, MULTIPLE, OPEN) else SINGLE)
            quiz.questions.append(question)
            return question.id

    def duplicate_question(self, code: str, question_id: str) -> str:
        with self._edit(code, frozen=True, layout=True) as quiz:
            if len(quiz.questions) >= MAX_QUESTIONS:
                raise StoreError(f"A quiz can have up to {MAX_QUESTIONS} questions.")
            index, original = self._question(quiz, question_id)
            mapping = {option.id: new_id() for option in original.options}
            copy = replace(original, id=new_id(), options=[Option(mapping[o.id], o.text) for o in original.options],
                           correct=[mapping[i] for i in original.correct if i in mapping],
                           accepted=list(original.accepted), reviews={})
            quiz.questions.insert(index + 1, copy)
            return copy.id

    def move_question(self, code: str, question_id: str, offset: int) -> None:
        with self._edit(code, frozen=True, layout=True) as quiz:
            index, question = self._question(quiz, question_id)
            target = max(0, min(len(quiz.questions) - 1, index + offset))
            quiz.questions.insert(target, quiz.questions.pop(index))

    def remove_question(self, code: str, question_id: str) -> None:
        with self._edit(code, frozen=True, layout=True) as quiz:
            index, _ = self._question(quiz, question_id)
            quiz.questions.pop(index)

    def update_question(self, code: str, question_id: str, **changes) -> None:
        """Change question fields. Text, type, options and 'required' are frozen once people joined."""
        answer_key = {"points", "partial_credit", "tolerant", "accepted"}
        with self._edit(code, frozen=bool(set(changes) - answer_key)) as quiz:
            _, question = self._question(quiz, question_id)
            for name, value in changes.items():
                if name == "text":
                    question.text = tx.clean(value, MAX_QUESTION, multiline=True)
                elif name == "kind":
                    if value not in (SINGLE, MULTIPLE, OPEN):
                        raise StoreError("Unknown question type.")
                    if value != OPEN and not question.options:
                        question.options = [Option(new_id()), Option(new_id())]
                    if value == SINGLE and len(question.correct) > 1:
                        question.correct = question.correct[:1]
                    question.kind = value
                elif name == "required":
                    question.required = bool(value)
                elif name == "points":
                    question.points = _bounded_int(value, 0, MAX_POINTS)
                elif name == "partial_credit":
                    question.partial_credit = bool(value)
                elif name == "tolerant":
                    question.tolerant = bool(value)
                elif name == "accepted":
                    values = value.splitlines() if isinstance(value, str) else list(value or [])
                    question.accepted = tx.clean_lines("\n".join(map(str, values)), MAX_ANSWER, MAX_ACCEPTED)
                else:
                    raise StoreError(f"Unknown question field: {name}.")

    def add_option(self, code: str, question_id: str) -> str:
        with self._edit(code, frozen=True, layout=True) as quiz:
            _, question = self._question(quiz, question_id)
            if len(question.options) >= MAX_OPTIONS:
                raise StoreError(f"A question can have up to {MAX_OPTIONS} options.")
            option = Option(new_id())
            question.options.append(option)
            return option.id

    def remove_option(self, code: str, question_id: str, option_id: str) -> None:
        with self._edit(code, frozen=True, layout=True) as quiz:
            _, question = self._question(quiz, question_id)
            question.options = [option for option in question.options if option.id != option_id]
            question.correct = [value for value in question.correct if value != option_id]

    def set_option_text(self, code: str, question_id: str, option_id: str, value: object) -> None:
        with self._edit(code, frozen=True) as quiz:
            _, question = self._question(quiz, question_id)
            option = question.option(option_id)
            if option is None:
                raise StoreError("That option no longer exists.")
            option.text = tx.clean(value, MAX_OPTION)

    def set_correct(self, code: str, question_id: str, option_id: str, right: bool) -> None:
        """Mark an option right or wrong. Allowed after people answered, to fix a wrong answer key."""
        with self._edit(code) as quiz:
            _, question = self._question(quiz, question_id)
            if question.option(option_id) is None:
                raise StoreError("That option no longer exists.")
            if not right:
                question.correct = [value for value in question.correct if value != option_id]
            elif question.kind == SINGLE:
                question.correct = [option_id]
            elif option_id not in question.correct:
                question.correct.append(option_id)

    def set_review(self, code: str, question_id: str, answer: str, right: Optional[bool]) -> None:
        """Override the automatic check of one open answer (None goes back to the automatic check)."""
        with self._edit(code) as quiz:
            _, question = self._question(quiz, question_id)
            key = tx.normalize(answer)
            if not key:
                return
            if right is None:
                question.reviews.pop(key, None)
            else:
                question.reviews[key] = bool(right)

    def update_settings(self, code: str, settings: Settings) -> None:
        with self._edit(code) as quiz:
            quiz.settings = self._sanitize_settings(settings)

    # ------------------------------------------------------------------ running

    def open(self, code: str, minutes: Optional[int] = None) -> None:
        """Open (or reopen) the quiz; ``minutes`` 0 keeps it open until closed by hand."""
        with self._edit(code) as quiz:
            problems = quiz.issues()
            if problems:
                raise StoreError("Fix the quiz before opening it: " + " ".join(problems))
            now = self.now()
            minutes = quiz.settings.time_limit_minutes if minutes is None else _bounded_int(
                minutes, 0, MAX_DURATION_MINUTES)
            quiz.opened_at = quiz.opened_at or now
            quiz.closed_at = None
            quiz.closes_at = now + minutes * 60 if minutes else None

    def extend(self, code: str, minutes: int) -> None:
        with self._edit(code) as quiz:
            now = self.now()
            if quiz.status(now) != OPEN_STATUS or quiz.closes_at is None:
                raise StoreError("Only a quiz that is open with a time limit can get more time.")
            quiz.closes_at = min(quiz.closes_at + _bounded_int(minutes, 1, MAX_DURATION_MINUTES) * 60,
                                 now + MAX_DURATION_MINUTES * 60)

    def close(self, code: str) -> None:
        with self._edit(code) as quiz:
            now = self.now()
            if quiz.status(now) == OPEN_STATUS:
                quiz.closed_at = now
                quiz.closes_at = now if quiz.closes_at is None else min(quiz.closes_at, now)

    def reset_responses(self, code: str) -> None:
        """Remove every participant and answer, and bring the quiz back to draft."""
        with self._edit(code, layout=True) as quiz:
            if quiz.status(self.now()) == OPEN_STATUS:
                raise StoreError("Close the quiz before removing its responses.")
            quiz.participants.clear()
            quiz.emails.clear()
            quiz.opened_at = quiz.closes_at = quiz.closed_at = None
            for question in quiz.questions:
                question.reviews.clear()

    def remove_participant(self, code: str, token: str) -> None:
        with self._edit(code) as quiz:
            participant = quiz.participants.pop(token, None)
            if participant and participant.email:
                quiz.emails.pop(participant.email, None)

    # ------------------------------------------------------------------ participants

    def join(self, code: str, name: object, email: object = "") -> Participant:
        with self._lock:
            quiz = self._require(code)
            now = self.now()
            status = quiz.status(now)
            if status == DRAFT:
                raise StoreError("This quiz hasn't started yet.")
            if status != OPEN_STATUS:
                raise StoreError("This quiz is closed.")
            name = tx.clean(name, MAX_NAME)
            if len(name) < 2:
                raise StoreError("Type your name (at least 2 characters).")
            email = tx.normalize_email(email) if quiz.settings.ask_email else ""
            if quiz.settings.ask_email:
                if not tx.valid_email(email):
                    raise StoreError("Type a valid email address.")
                if not tx.domain_allowed(email, quiz.settings.email_domains):
                    allowed = ", ".join(quiz.settings.email_domains)
                    raise StoreError(f"This quiz only accepts emails from: {allowed}.")
                if email in quiz.emails:
                    raise StoreError("This email already joined this quiz. Continue on the device and browser "
                                     "where you started.")
            if len(quiz.participants) >= self._max_participants:
                raise StoreError("This quiz is full.")
            if sum(len(q.participants) for q in self._quizzes.values()) >= self._max_total_participants:
                raise StoreError("The app is busy right now. Please try again in a moment.")
            participant = Participant(token=secrets.token_urlsafe(16), name=name, email=email, joined_at=now)
            quiz.participants[participant.token] = participant
            if email:
                quiz.emails[email] = participant.token
            self._touch(quiz)
            return replace(participant, shown_at={}, answers={})

    def show(self, code: str, token: str, question_id: str) -> float:
        """Record when a participant first saw a question (the answer time counts from here)."""
        with self._lock:
            quiz, participant = self._require_participant(code, token)
            self._require_open(quiz)
            question = self._next_question(quiz, participant)
            if question is None or question.id != question_id:
                raise StoreError("That question isn't the next one for you.")
            if question_id not in participant.shown_at:
                participant.shown_at[question_id] = self.now()
                self._touch(quiz)
            return participant.shown_at[question_id]

    def answer(self, code: str, token: str, question_id: str, choice: Iterable[str] = (), text: object = "",
               skipped: bool = False) -> Answer:
        with self._lock:
            quiz, participant = self._require_participant(code, token)
            if question_id in participant.answers:   # double click or resent form: keep the first answer
                return participant.answers[question_id]
            self._require_open(quiz, grace=GRACE_SECONDS)
            question = self._next_question(quiz, participant)
            if question is None or question.id != question_id:
                raise StoreError("That question isn't the next one for you.")
            now = self.now()
            valid = {option.id for option in question.options}
            picked = [] if skipped or question.kind == OPEN else list(dict.fromkeys(c for c in choice if c in valid))
            typed = "" if skipped or question.kind != OPEN else tx.clean(text, MAX_ANSWER, multiline=True)
            if question.kind == SINGLE and len(picked) > 1:
                raise StoreError("Pick only one option.")
            if skipped and question.required:
                raise StoreError("This question is required.")
            if not skipped and not picked and not typed:
                if question.required:
                    raise StoreError("Pick an option." if question.is_choice else "Type your answer.")
                skipped = True
            answer = Answer(question_id=question_id, shown_at=participant.shown_at.get(question_id, now),
                            answered_at=now, choice=picked, text=typed, skipped=skipped)
            participant.answers[question_id] = answer
            if self._next_question(quiz, participant) is None:
                participant.finished_at = now
            self._touch(quiz)
            return answer

    def participant(self, code: str, token: object) -> Optional[Participant]:
        with self._lock:
            quiz = self._quizzes.get(tx.only_digits(code))
            participant = quiz.participants.get(str(token or "")) if quiz else None
            if participant is None:
                return None
            return replace(participant, shown_at=dict(participant.shown_at), answers=dict(participant.answers))

    @staticmethod
    def next_question(quiz: Quiz, participant: Participant) -> Optional[Question]:
        return QuizStore._next_question(quiz, participant)

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _next_question(quiz: Quiz, participant: Participant) -> Optional[Question]:
        return next((question for question in quiz.questions if question.id not in participant.answers), None)

    def _new_code(self) -> str:
        for _ in range(1000):
            code = str(secrets.randbelow(900000) + 100000)   # six digits, never starting with 0
            if code not in self._quizzes and code not in self._used_codes:
                self._used_codes.add(code)
                return code
        raise StoreError("Couldn't create a quiz code. Please try again.")

    def _require(self, code: object) -> Quiz:
        quiz = self._quizzes.get(tx.only_digits(code))
        if quiz is None:
            raise StoreError("Quiz not found. It may have expired: quizzes are temporary.")
        return quiz

    def _require_participant(self, code: str, token: str) -> Tuple[Quiz, Participant]:
        quiz = self._require(code)
        participant = quiz.participants.get(str(token or ""))
        if participant is None:
            raise StoreError("We couldn't find your answers. Join the quiz again.")
        return quiz, participant

    def _require_open(self, quiz: Quiz, grace: float = 0.0) -> None:
        now = self.now()
        if quiz.opened_at is None:
            raise StoreError("This quiz hasn't started yet.")
        late = quiz.closes_at is not None and now >= quiz.closes_at + grace
        if quiz.closed_at is not None or late:
            raise StoreError("Time's up: this quiz is closed.")

    def _question(self, quiz: Quiz, question_id: str) -> Tuple[int, Question]:
        for index, question in enumerate(quiz.questions):
            if question.id == question_id:
                return index, question
        raise StoreError("That question no longer exists.")

    def _edit(self, code: str, frozen: bool = False, layout: bool = False) -> "_Edit":
        return _Edit(self, code, frozen, layout)

    def _touch(self, quiz: Quiz, changed: bool = True) -> None:
        quiz.updated_at = self.now()
        if changed:
            quiz.revision += 1

    def _sweep(self, force: bool = False) -> None:
        now = self.now()
        if not force and now - self._last_sweep < SWEEP_EVERY:
            return
        self._last_sweep = now
        for code in [code for code, quiz in self._quizzes.items() if now - quiz.updated_at > self._ttl]:
            del self._quizzes[code]

    @staticmethod
    def _sanitize_question(question: Question) -> Question:
        kind = question.kind if question.kind in (SINGLE, MULTIPLE, OPEN) else SINGLE
        options = [Option(new_id(), tx.clean(option.text, MAX_OPTION)) for option in question.options[:MAX_OPTIONS]]
        mapping = {old.id: new.id for old, new in zip(question.options, options)}
        correct = list(dict.fromkeys(mapping[c] for c in question.correct if c in mapping))
        if kind == SINGLE:
            correct = correct[:1]
        return Question(
            id=new_id(), kind=kind, text=tx.clean(question.text, MAX_QUESTION, multiline=True),
            options=options if kind != OPEN else [], correct=correct if kind != OPEN else [],
            accepted=tx.clean_lines("\n".join(question.accepted), MAX_ANSWER, MAX_ACCEPTED) if kind == OPEN else [],
            required=bool(question.required), points=_bounded_int(question.points, 0, MAX_POINTS),
            partial_credit=bool(question.partial_credit) and kind == MULTIPLE, tolerant=bool(question.tolerant),
        )

    @staticmethod
    def _sanitize_settings(settings: Settings) -> Settings:
        not_eligible = [tx.normalize_email(value) for value in settings.not_eligible]
        return Settings(
            time_limit_minutes=_bounded_int(settings.time_limit_minutes, 0, MAX_DURATION_MINUTES),
            ask_email=bool(settings.ask_email),
            email_domains=tx.clean_domains(" ".join(settings.email_domains)),
            shuffle_options=bool(settings.shuffle_options),
            ranking=settings.ranking if settings.ranking in RANKINGS else Settings().ranking,
            bonus_window_seconds=_bounded_int(settings.bonus_window_seconds, 5, 600),
            winners=_bounded_int(settings.winners, 1, MAX_WINNERS),
            min_score_percent=_bounded_int(settings.min_score_percent, 0, 100),
            require_finished=bool(settings.require_finished),
            not_eligible=list(dict.fromkeys(e for e in not_eligible if tx.valid_email(e)))[:MAX_NOT_ELIGIBLE],
        )

    @staticmethod
    def _copy(quiz: Quiz) -> Quiz:
        questions = [replace(q, options=[replace(o) for o in q.options], correct=list(q.correct),
                             accepted=list(q.accepted), reviews=dict(q.reviews)) for q in quiz.questions]
        settings = replace(quiz.settings, email_domains=list(quiz.settings.email_domains),
                           not_eligible=list(quiz.settings.not_eligible))
        participants = {token: replace(p, shown_at=dict(p.shown_at), answers=dict(p.answers))
                        for token, p in quiz.participants.items()}
        return replace(quiz, questions=questions, settings=settings, participants=participants,
                       emails=dict(quiz.emails))


class _Edit:
    """``with store._edit(code) as quiz``: lock, find the quiz, check edit rules, then record the change.

    ``frozen`` changes are refused once people joined. ``layout`` changes add, remove or move questions or
    options, so editors rebuild their widgets.
    """

    def __init__(self, store: QuizStore, code: str, frozen: bool, layout: bool):
        self.store, self.code, self.frozen, self.layout = store, code, frozen, layout

    def __enter__(self) -> Quiz:
        self.store._lock.acquire()
        try:
            quiz = self.store._require(self.code)
            if self.frozen and quiz.locked:
                raise StoreError("People already joined this quiz, so questions, options and the 'required' "
                                 "setting can't change. You can still fix right answers and points.")
            self.quiz = quiz
            return quiz
        except BaseException:
            self.store._lock.release()
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.store._touch(self.quiz)
                if self.layout:
                    self.quiz.layout_revision += 1
        finally:
            self.store._lock.release()


def _bounded_int(value: object, low: int, high: int) -> int:
    try:
        number = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = low
    return max(low, min(high, number))


_STORE: Optional[QuizStore] = None
_STORE_LOCK = threading.Lock()


def shared_store() -> QuizStore:
    """The one store every session of this process uses."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = QuizStore()
        return _STORE


__all__: List[str] = ["QuizStore", "StoreError", "shared_store", "new_admin_key", "normalize_admin_key",
                      "default_question", "TTL_HOURS", "GRACE_SECONDS"]

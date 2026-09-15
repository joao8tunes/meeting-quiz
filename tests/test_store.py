import threading

import pytest

from quiz.models import CLOSED, DRAFT, MULTIPLE, OPEN_STATUS, SINGLE, Settings
from quiz.store import GRACE_SECONDS, QuizStore, StoreError, new_admin_key, normalize_admin_key

from helpers import Clock, answer_all, make_store, small_quiz


def test_codes_have_six_digits_and_admin_keys_are_long_random_and_hashed():
    store = QuizStore()
    codes = set()
    for _ in range(50):
        quiz, key = store.create("Quiz")
        assert len(quiz.code) == 6 and quiz.code.isdigit() and quiz.code[0] != "0"
        assert len(normalize_admin_key(key)) == 12
        assert normalize_admin_key(key).encode() not in quiz.admin_hash
        codes.add(quiz.code)
    assert len(codes) == 50
    assert new_admin_key() != new_admin_key()


def test_admin_key_is_required_and_forgiving_about_format():
    store, _, code, key = make_store()
    assert store.authenticate(code, key) is not None
    assert store.authenticate(code[:3] + " " + code[3:], key.lower().replace("-", " ")) is not None
    assert store.authenticate(code, "WRONG-WRONG-WRONG") is None
    assert store.authenticate(code, "") is None
    assert store.authenticate("000000", key) is None


def test_get_returns_a_copy_that_cannot_change_the_stored_quiz():
    store, _, code, _ = make_store()
    copy = store.get(code)
    copy.title = "Hacked"
    copy.questions[0].correct.clear()
    copy.questions.pop()
    fresh = store.get(code)
    assert fresh.title == "Test quiz"
    assert fresh.questions[0].correct and len(fresh.questions) == 4


def test_nobody_joins_a_draft_or_closed_quiz():
    store, clock, code, _ = make_store(time_limit_minutes=1)
    with pytest.raises(StoreError, match="hasn't started"):
        store.join(code, "Player One", "player.one@example.com")
    store.open(code)
    assert store.get(code).status(clock()) == OPEN_STATUS
    clock.advance(61)
    assert store.get(code).status(clock()) == CLOSED
    with pytest.raises(StoreError, match="closed"):
        store.join(code, "Player One", "player.one@example.com")


def test_join_validates_name_email_domain_and_duplicates():
    store, _, code, _ = make_store(email_domains=["example.com"])
    store.open(code)
    with pytest.raises(StoreError, match="name"):
        store.join(code, " ", "a@example.com")
    with pytest.raises(StoreError, match="valid email"):
        store.join(code, "Player One", "not-an-email")
    with pytest.raises(StoreError, match="only accepts emails"):
        store.join(code, "Player One", "player.one@elsewhere.org")
    store.join(code, "Player One", "Player.One@Example.com")
    with pytest.raises(StoreError, match="already joined"):
        store.join(code, "Someone else", "player.one@example.com")


def test_without_emails_people_only_need_a_name():
    store, _, code, _ = make_store(ask_email=False)
    store.open(code)
    first = store.join(code, "Player One", "ignored@example.com")
    second = store.join(code, "Player One", "")
    assert first.email == second.email == ""
    assert first.token != second.token


def test_quiz_limits_protect_the_shared_instance():
    clock = Clock()
    store = QuizStore(clock=clock, max_quizzes=2, max_participants=1)
    quiz, _ = store.create("One", questions=small_quiz())
    store.create("Two")
    with pytest.raises(StoreError, match="too many quizzes"):
        store.create("Three")
    store.open(quiz.code)
    store.join(quiz.code, "Player One", "player.one@example.com")
    with pytest.raises(StoreError, match="full"):
        store.join(quiz.code, "Player Two", "player.two@example.com")


def test_questions_are_answered_in_order_one_at_a_time():
    store, clock, code, _ = make_store()
    store.open(code)
    person = store.join(code, "Player One", "player.one@example.com")
    quiz = store.get(code)
    first, second = quiz.questions[0], quiz.questions[1]
    with pytest.raises(StoreError, match="next one"):
        store.show(code, person.token, second.id)
    with pytest.raises(StoreError, match="next one"):
        store.answer(code, person.token, second.id, choice=[second.correct[0]])
    shown = store.show(code, person.token, first.id)
    clock.advance(3.5)
    assert store.show(code, person.token, first.id) == shown   # reloading doesn't restart the timer
    with pytest.raises(StoreError, match="required"):
        store.answer(code, person.token, first.id, skipped=True)
    with pytest.raises(StoreError, match="Pick an option"):
        store.answer(code, person.token, first.id)
    with pytest.raises(StoreError, match="only one"):
        store.answer(code, person.token, first.id, choice=[o.id for o in first.options])
    answer = store.answer(code, person.token, first.id, choice=[first.correct[0], "not-an-option"])
    assert answer.seconds == pytest.approx(3.5)
    assert answer.choice == [first.correct[0]]
    again = store.answer(code, person.token, first.id, choice=[first.options[0].id])   # double submit
    assert again.choice == [first.correct[0]]


def test_optional_questions_can_be_skipped_and_finishing_is_recorded():
    store, clock, code, _ = make_store()
    store.open(code)
    person = store.join(code, "Player One", "player.one@example.com")
    answer_all(store, clock, code, person.token)
    stored = store.participant(code, person.token)
    assert stored.finished_at is not None
    assert stored.answers[store.get(code).questions[-1].id].skipped


def test_answers_after_the_time_limit_are_refused_except_within_the_grace_period():
    store, clock, code, _ = make_store(time_limit_minutes=1)
    store.open(code)
    person = store.join(code, "Player One", "player.one@example.com")
    question = store.get(code).questions[0]
    store.show(code, person.token, question.id)
    clock.advance(60 + GRACE_SECONDS / 2)
    store.answer(code, person.token, question.id, choice=question.correct)
    second = store.get(code).questions[1]
    clock.advance(GRACE_SECONDS)
    with pytest.raises(StoreError, match="Time's up"):
        store.answer(code, person.token, second.id, choice=second.correct)


def test_closing_by_hand_extending_and_reopening():
    store, clock, code, _ = make_store(time_limit_minutes=5)
    store.open(code)
    store.extend(code, 5)
    assert store.get(code).remaining(clock()) == pytest.approx(600)
    store.close(code)
    quiz = store.get(code)
    assert quiz.status(clock()) == CLOSED
    with pytest.raises(StoreError):
        store.extend(code, 1)
    store.open(code, minutes=0)
    quiz = store.get(code)
    assert quiz.status(clock()) == OPEN_STATUS and quiz.closes_at is None
    clock.advance(10 * 3600)
    assert store.get(code).status(clock()) == OPEN_STATUS


def test_opening_requires_a_valid_quiz():
    store = QuizStore()
    quiz, _ = store.create("Empty")
    with pytest.raises(StoreError, match="at least one question"):
        store.open(quiz.code)
    store.add_question(quiz.code, SINGLE)
    with pytest.raises(StoreError, match="no text"):
        store.open(quiz.code)
    assert store.get(quiz.code).status(store.now()) == DRAFT


def test_questions_lock_once_people_join_but_answer_keys_can_still_be_fixed():
    store, clock, code, _ = make_store()
    store.open(code)
    store.join(code, "Player One", "player.one@example.com")
    question = store.get(code).questions[0]
    for change in (lambda: store.add_question(code, SINGLE), lambda: store.remove_question(code, question.id),
                   lambda: store.update_question(code, question.id, text="Changed"),
                   lambda: store.update_question(code, question.id, required=False),
                   lambda: store.set_option_text(code, question.id, question.options[0].id, "Changed"),
                   lambda: store.add_option(code, question.id), lambda: store.move_question(code, question.id, 1)):
        with pytest.raises(StoreError, match="already joined"):
            change()
    wrong = next(o.id for o in question.options if o.id not in question.correct)
    store.set_correct(code, question.id, wrong, True)
    store.update_question(code, question.id, points=50)
    fixed = store.get(code).questions[0]
    assert fixed.correct == [wrong] and fixed.points == 50


def test_resetting_responses_unlocks_the_quiz_and_brings_it_back_to_draft():
    store, clock, code, _ = make_store()
    store.open(code)
    store.join(code, "Player One", "player.one@example.com")
    with pytest.raises(StoreError, match="Close the quiz"):
        store.reset_responses(code)
    store.close(code)
    store.reset_responses(code)
    quiz = store.get(code)
    assert quiz.status(clock()) == DRAFT and not quiz.participants and not quiz.locked
    store.update_question(code, quiz.questions[0].id, text="Now editable")


def test_single_choice_keeps_one_right_answer_and_type_changes_keep_options_valid():
    store, _, code, _ = make_store()
    question = store.get(code).questions[1]   # multiple choice with two right answers
    store.update_question(code, question.id, kind=SINGLE)
    assert len(store.get(code).questions[1].correct) == 1
    store.set_correct(code, question.id, question.options[1].id, True)
    assert store.get(code).questions[1].correct == [question.options[1].id]
    store.update_question(code, question.id, kind=MULTIPLE)
    store.set_correct(code, question.id, question.options[0].id, True)
    assert len(store.get(code).questions[1].correct) == 2


def test_editing_helpers_duplicate_move_and_remove():
    store, _, code, _ = make_store()
    quiz = store.get(code)
    copy_id = store.duplicate_question(code, quiz.questions[0].id)
    quiz = store.get(code)
    assert quiz.questions[1].id == copy_id and quiz.questions[1].text == quiz.questions[0].text
    assert set(o.id for o in quiz.questions[1].options).isdisjoint(o.id for o in quiz.questions[0].options)
    store.move_question(code, copy_id, 10)
    assert store.get(code).questions[-1].id == copy_id
    store.remove_question(code, copy_id)
    assert all(q.id != copy_id for q in store.get(code).questions)
    before = store.get(code).layout_revision
    store.update_question(code, quiz.questions[0].id, text="Only text")
    assert store.get(code).layout_revision == before


def test_settings_are_cleaned():
    store, _, code, _ = make_store()
    store.update_settings(code, Settings(winners=0, min_score_percent=250, ranking="unknown", time_limit_minutes=-3,
                                         email_domains=["@Example.com", "bad domain"],
                                         not_eligible=["Boss@Example.com", "nope"]))
    settings = store.get(code).settings
    assert settings.winners == 1 and settings.min_score_percent == 100 and settings.time_limit_minutes == 0
    assert settings.ranking == Settings().ranking
    assert settings.email_domains == ["example.com"] and settings.not_eligible == ["boss@example.com"]


def test_inactive_quizzes_are_deleted_after_the_ttl():
    clock = Clock()
    store = QuizStore(clock=clock, ttl_hours=1)
    old, _ = store.create("Old")
    clock.advance(1800)
    recent, _ = store.create("Recent")
    clock.advance(1900)
    assert store.get(old.code) is None
    assert store.get(recent.code) is not None
    assert len(store) == 1


def test_removing_a_participant_frees_their_email():
    store, _, code, _ = make_store()
    store.open(code)
    person = store.join(code, "Player One", "player.one@example.com")
    store.remove_participant(code, person.token)
    assert store.participant(code, person.token) is None
    store.join(code, "Player One", "player.one@example.com")


def test_many_people_answering_at_the_same_time():
    store, clock, code, _ = make_store(ask_email=False)
    store.open(code)
    errors = []

    def person(index):
        try:
            joined = store.join(code, f"Person {index}")
            answer_all(store, clock, code, joined.token, right=index % 2 == 0, seconds=0)
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=person, args=(i,)) for i in range(60)]
    for thread in threads:
        thread.start()
    reader_errors = []

    def reader():
        try:
            for _ in range(200):
                from quiz.scoring import rank

                rank(store.get(code))
        except Exception as error:  # noqa: BLE001
            reader_errors.append(error)

    readers = [threading.Thread(target=reader) for _ in range(3)]
    for thread in readers:
        thread.start()
    for thread in threads + readers:
        thread.join()
    assert not errors and not reader_errors
    quiz = store.get(code)
    assert len(quiz.participants) == 60
    assert all(p.finished_at is not None for p in quiz.participants.values())

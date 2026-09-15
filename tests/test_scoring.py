import pytest

from quiz.models import MULTIPLE, OPEN, SPEED_BONUS, Answer, Question, Settings, new_id
from quiz.scoring import describe_rules, grade, open_answer_right, rank

from helpers import answer_all, choice, make_store


def answer(choice_ids=(), text="", seconds=5.0, skipped=False):
    return Answer(question_id="q", shown_at=100.0, answered_at=100.0 + seconds, choice=list(choice_ids), text=text,
                  skipped=skipped)


def test_single_choice_is_all_or_nothing():
    question = choice("single", "?", [("a", False), ("b", True)])
    right, wrong = question.correct[0], question.options[0].id
    assert grade(question, answer([right]), Settings()).points == 100
    assert grade(question, answer([wrong]), Settings()).points == 0
    assert grade(question, None, Settings()).right is False
    assert grade(question, answer(skipped=True), Settings()).points == 0


def test_multiple_choice_exact_or_partial_credit():
    question = choice(MULTIPLE, "?", [("a", True), ("b", True), ("c", False), ("d", False)])
    a, b, c, d = (o.id for o in question.options)
    assert grade(question, answer([a, b]), Settings()).points == 100
    assert grade(question, answer([a]), Settings()).points == 0
    question.partial_credit = True
    assert grade(question, answer([a]), Settings()).points == 50
    assert grade(question, answer([a, c]), Settings()).points == 0          # a wrong pick cancels a right one
    assert grade(question, answer([a, b, c]), Settings()).points == 50
    assert grade(question, answer([a, b, c, d]), Settings()).points == 0    # picking everything doesn't pay
    assert grade(question, answer([a, b]), Settings()).right is True


def test_open_answers_use_accepted_answers_typos_and_admin_reviews():
    question = Question(id=new_id(), kind=OPEN, text="?", accepted=["Jupiter", "Júpiter"])
    assert open_answer_right(question, " jupiter! ")
    assert open_answer_right(question, "Jupyter")
    question.tolerant = False
    assert not open_answer_right(question, "Jupyter")
    question.reviews["jupyter"] = True
    assert open_answer_right(question, "JUPYTER")
    question.reviews["jupiter"] = False
    assert not open_answer_right(question, "Jupiter")


def test_polls_and_feedback_questions_never_count():
    question = Question(id=new_id(), kind=OPEN, text="?", points=0)
    result = grade(question, answer(text="anything"), Settings())
    assert result.right is None and result.points == 0


def test_speed_bonus_rewards_fast_right_answers_only():
    question = choice("single", "?", [("a", True), ("b", False)])
    settings = Settings(ranking=SPEED_BONUS, bonus_window_seconds=20)
    instant = grade(question, answer([question.correct[0]], seconds=0), settings)
    halfway = grade(question, answer([question.correct[0]], seconds=10), settings)
    slow = grade(question, answer([question.correct[0]], seconds=60), settings)
    wrong = grade(question, answer([question.options[1].id], seconds=0), settings)
    assert instant.points == pytest.approx(150)
    assert halfway.points == pytest.approx(125)
    assert slow.points == pytest.approx(100)
    assert wrong.points == 0


def test_ranking_uses_points_then_time_and_winners_follow_the_rules():
    store, clock, code, _ = make_store(winners=2)
    store.open(code)
    slow = store.join(code, "Slow", "slow@example.com")
    fast = store.join(code, "Fast", "fast@example.com")
    wrong = store.join(code, "Wrong", "wrong@example.com")
    answer_all(store, clock, code, slow.token, right=True, seconds=5)
    answer_all(store, clock, code, fast.token, right=True, seconds=1)
    answer_all(store, clock, code, wrong.token, right=False, seconds=1)
    results = rank(store.get(code))
    assert [row.name for row in results.rows] == ["Fast", "Slow", "Wrong"]
    assert [row.name for row in results.winners] == ["Fast", "Slow"]
    top = results.rows[0]
    assert top.points == 300 and top.percent == 100 and top.right == 3 and top.finished
    assert results.rows[2].eligible is False and results.rows[2].reason == "No points"


def test_eligibility_rules_skip_people_and_move_the_prize_down():
    store, clock, code, _ = make_store(winners=1, not_eligible=["boss@example.com"], min_score_percent=60)
    store.open(code)
    boss = store.join(code, "Boss", "boss@example.com")
    halfway = store.join(code, "Halfway", "halfway@example.com")
    low = store.join(code, "Low", "low@example.com")
    winner = store.join(code, "Winner", "winner@example.com")
    answer_all(store, clock, code, boss.token, seconds=0.5)
    quiz = store.get(code)
    first = quiz.questions[0]
    store.show(code, halfway.token, first.id)
    store.answer(code, halfway.token, first.id, choice=first.correct)
    answer_all(store, clock, code, winner.token, seconds=3)
    for question in quiz.questions:   # right only on the first question: 100 of 300 points
        store.show(code, low.token, question.id)
        if question.id == first.id:
            store.answer(code, low.token, question.id, choice=question.correct)
        elif question.kind == OPEN:
            store.answer(code, low.token, question.id, text="venus" if question.required else "", skipped=False)
        else:
            store.answer(code, low.token, question.id, choice=[o.id for o in question.options
                                                               if o.id not in question.correct][:1])
    rows = {row.name: row for row in rank(store.get(code)).rows}
    assert rows["Boss"].rank == 1 and not rows["Boss"].eligible
    assert rows["Halfway"].reason == "Didn't finish"
    assert rows["Low"].reason == "Below 60%"
    assert rows["Winner"].winner and rank(store.get(code)).winners[0].name == "Winner"


def test_fixing_the_answer_key_after_the_quiz_updates_the_ranking():
    store, clock, code, _ = make_store()
    store.open(code)
    person = store.join(code, "Player One", "player.one@example.com")
    answer_all(store, clock, code, person.token, right=False)
    assert rank(store.get(code)).rows[0].points == 0
    quiz = store.get(code)
    first = quiz.questions[0]
    picked = store.participant(code, person.token).answers[first.id].choice[0]
    store.set_correct(code, first.id, picked, True)
    open_question = quiz.questions[2]
    store.set_review(code, open_question.id, "Venus", True)
    assert rank(store.get(code)).rows[0].points == 200


def test_question_stats_count_options_and_group_open_answers():
    store, clock, code, _ = make_store()
    store.open(code)
    for index, right in enumerate([True, True, False]):
        person = store.join(code, f"P{index}", f"p{index}@example.com")
        answer_all(store, clock, code, person.token, right=right)
    stats = rank(store.get(code)).questions
    single = stats[0]
    assert single.seen == 3 and single.right == 2 and single.percent_right == pytest.approx(66.666, rel=1e-3)
    assert sum(option.count for option in single.options) == 3
    open_stats = stats[2]
    assert {(g.key, g.count, g.right) for g in open_stats.open_groups} == {("mars", 2, True), ("venus", 1, False)}
    assert stats[3].percent_right is None


def test_rules_are_described_in_plain_language():
    rules = describe_rules(Settings(winners=3, min_score_percent=50, not_eligible=["a@example.com"]), 300)
    assert rules[0] == "3 winners"
    assert "at least 50% of the points" in rules
    assert "1 email not eligible" in rules

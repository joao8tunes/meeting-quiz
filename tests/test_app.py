import html
from pathlib import Path

import pytest

pytest.importorskip("streamlit.testing.v1")
from streamlit.testing.v1 import AppTest  # noqa: E402

if not hasattr(AppTest, "segmented_control"):
    pytest.skip("this Streamlit version's AppTest can't drive segmented controls", allow_module_level=True)

import quiz.store as store_module  # noqa: E402
from quiz.models import OPEN, SINGLE  # noqa: E402
from quiz.samples import sample_quiz  # noqa: E402

APP = str(Path(__file__).resolve().parent.parent / "streamlit_app.py")
ADMIN_VIEWS = ["✏️ Build", "⚙️ Settings", "🚀 Run", "🏆 Results"]
RESULT_VIEWS = ["Ranking", "Winners", "Questions", "Review answers", "Downloads"]


@pytest.fixture(autouse=True)
def fresh_store(monkeypatch):
    monkeypatch.setattr(store_module, "_STORE", None)
    yield
    store_module._STORE = None


def run(at: AppTest) -> AppTest:
    at.run()
    assert not at.exception, [error.value for error in at.exception]
    return at


def button(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


def page_text(at: AppTest) -> str:
    parts = []
    for kind in ("markdown", "caption", "info", "success", "warning", "error", "title", "header", "subheader"):
        parts.extend(str(element.value) for element in getattr(at, kind))
    for radio in at.radio:
        parts.extend(map(str, radio.options))
    parts.extend(checkbox.label for checkbox in at.checkbox)
    return "\n".join(parts)


def create_sample(at: AppTest) -> str:
    at.segmented_control(key="home_action").set_value("Create a quiz")
    run(at)
    button(at, "Create sample quiz").click()
    run(at)
    return at.session_state["admin_code"]


@pytest.mark.parametrize("action", ["Join a quiz", "Create a quiz", "Manage a quiz"])
def test_home_page_actions_render(action):
    at = run(AppTest.from_file(APP, default_timeout=30))
    at.segmented_control(key="home_action").set_value(action)
    run(at)
    assert "Temporary by design" in page_text(at)


def test_wrong_code_and_wrong_admin_key_are_refused():
    at = run(AppTest.from_file(APP, default_timeout=30))
    at.text_input[0].input("999 999")
    button(at, "Join").click()
    run(at)
    assert "There's no quiz with this code" in page_text(at)

    store = store_module.shared_store()
    quiz, key = store.create("Private quiz")
    at.segmented_control(key="home_action").set_value("Manage a quiz")
    run(at)
    at.text_input[0].input(quiz.code)
    at.text_input[1].input("WRONG-KEYS-HERE")
    button(at, "Open admin area").click()
    run(at)
    assert "wrong" in page_text(at)
    assert "admin_code" not in at.session_state
    at.text_input[1].input(key)
    button(at, "Open admin area").click()
    run(at)
    assert at.session_state["admin_code"] == quiz.code


def test_creating_a_quiz_shows_the_admin_key_once_and_every_admin_view_renders():
    at = run(AppTest.from_file(APP, default_timeout=30))
    code = create_sample(at)
    assert "Save your admin key now" in page_text(at)
    button(at, "I saved my admin key").click()
    run(at)
    assert "Save your admin key now" not in page_text(at)
    for view in ADMIN_VIEWS:
        at.segmented_control(key="admin_view").set_value(view)
        run(at)
    assert store_module.shared_store().get(code).status(0) == "draft"


def test_admin_edits_reach_the_store():
    at = run(AppTest.from_file(APP, default_timeout=30))
    code = create_sample(at)
    store = store_module.shared_store()
    first = store.get(code).questions[0]
    points = next(n for n in at.number_input if n.label == "Points")
    points.set_value(40)
    run(at)
    assert store.get(code).questions[0].points == 40
    required = next(t for t in at.toggle if t.label == "Required")
    required.set_value(False)
    run(at)
    assert store.get(code).questions[0].required is False
    button(at, "Open answer").click()
    run(at)
    assert store.get(code).questions[-1].kind == OPEN
    assert store.get(code).questions[0].id == first.id


def test_a_full_quiz_from_joining_to_results():
    admin = run(AppTest.from_file(APP, default_timeout=30))
    code = create_sample(admin)
    admin.segmented_control(key="admin_view").set_value("🚀 Run")
    run(admin)
    button(admin, "Open quiz").click()
    run(admin)
    store = store_module.shared_store()
    assert store.get(code).status(store.now()) == "open"

    person = AppTest.from_file(APP, default_timeout=30)
    person.query_params["quiz"] = code
    run(person)
    person.text_input[0].input("Player One")
    person.text_input[1].input("player.one@example.com")
    button(person, "Start quiz").click()
    run(person)

    quiz = store.get(code)
    accepted = [answer for q in quiz.questions if q.kind == OPEN for answer in q.accepted]
    for question in quiz.questions:
        shown = page_text(person)
        assert html.escape(question.text) in shown
        for answer in accepted:
            assert answer not in shown, "accepted answers must never reach participants"
        if question.kind == SINGLE:
            person.radio[0].set_value(question.correct[0])
        elif question.kind == OPEN:
            person.text_area[0].input("Mars" if question.required else "More quizzes")
        else:
            for checkbox in person.checkbox:
                checkbox.check()
        label = "Finish" if question is quiz.questions[-1] else "Next"
        button(person, label).click()
        run(person)
    assert "All done" in page_text(person)
    participant = next(iter(store.get(code).participants.values()))
    assert participant.finished_at is not None

    admin.segmented_control(key="admin_view").set_value("🏆 Results")
    run(admin)
    for view in RESULT_VIEWS:
        admin.segmented_control(key="results_view").set_value(view)
        run(admin)
    assert "player.one@example.com" not in page_text(admin)


def test_participants_see_a_friendly_message_for_unknown_or_closed_quizzes():
    at = AppTest.from_file(APP, default_timeout=30)
    at.query_params["quiz"] = "123456"
    run(at)
    assert "There's no quiz with this code" in page_text(at)

    store = store_module.shared_store()
    quiz, _ = store.create(*sample_quiz())
    at = AppTest.from_file(APP, default_timeout=30)
    at.query_params["quiz"] = quiz.code
    run(at)
    assert "hasn't started yet" in page_text(at)
    store.open(quiz.code)
    store.close(quiz.code)
    at = AppTest.from_file(APP, default_timeout=30)
    at.query_params["quiz"] = quiz.code
    run(at)
    assert "This quiz is closed" in page_text(at)

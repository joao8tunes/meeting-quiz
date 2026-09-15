"""Meeting Quiz: quick quizzes for live events and online meetings, ranked by right answers and speed."""

from __future__ import annotations

import hashlib
import html
import inspect
import io
import os
import random
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional, Sequence
from urllib.parse import urlsplit, urlunsplit

import pandas as pd
import streamlit as st

from quiz import export, quizfile, widgets
from quiz import text as tx
from quiz.models import (
    CLOSED, DRAFT, KIND_HELP, KINDS, MAX_ANSWER, MAX_DESCRIPTION, MAX_DURATION_MINUTES, MAX_EMAIL, MAX_NAME,
    MAX_OPTION, MAX_OPTIONS, MAX_POINTS, MAX_QUESTION, MAX_QUESTIONS, MAX_TITLE, MAX_WINNERS, MULTIPLE, OPEN,
    OPEN_STATUS, RANKING_HELP, RANKINGS, SINGLE, SPEED_BONUS, STATUS_LABELS, Participant, Question, Quiz, Settings,
)
from quiz.samples import sample_quiz
from quiz.scoring import Results, describe_rules, progress, rank
from quiz.store import QuizStore, StoreError, shared_store

APP_NAME = "Meeting Quiz"
VIEWS = ["✏️ Build", "⚙️ Settings", "🚀 Run", "🏆 Results"]
RESULT_VIEWS = ["Ranking", "Winners", "Questions", "Review answers", "Downloads"]
HOME_ACTIONS = ["Join a quiz", "Create a quiz", "Manage a quiz"]
STATUS_COLORS = {DRAFT: "gray", OPEN_STATUS: "green", CLOSED: "orange"}
CREATE_LIMIT = (10, 3600)       # quizzes per session per hour
JOIN_LIMIT = (8, 60)            # wrong codes per session per minute
ADMIN_LIMIT = (5, 300)          # wrong admin keys per session per 5 minutes

CSS = """
<style>
.join-card{text-align:center;padding:.5rem 0}
.join-card .label{font-size:1.05rem;opacity:.75;margin:0}
.join-card .url{font-size:clamp(1rem,1.9vw,1.6rem);font-weight:600;overflow-wrap:anywhere;margin:.1rem 0 .9rem}
.join-card .code{font-size:clamp(3rem,8vw,5.5rem);font-weight:800;letter-spacing:.08em;line-height:1;
  font-variant-numeric:tabular-nums;margin:.1rem 0}
.quiz-question{font-size:1.35rem;font-weight:600;line-height:1.35;margin:.2rem 0 .6rem}
[data-testid="stButtonGroup"]>div{flex-wrap:wrap;row-gap:.5rem}
</style>
"""


# ---------------------------------------------------------------------------- helpers

def store() -> QuizStore:
    return shared_store()


def keep_selected(key: str, options: Sequence[str], default: str) -> None:
    """Segmented controls let people unselect the active option: keep their last choice instead."""
    remembered = f"last_{key}"
    if st.session_state.get(key) not in options:
        last = st.session_state.get(remembered)
        st.session_state[key] = last if last in options else default
    st.session_state[remembered] = st.session_state[key]


def flash(message: str, kind: str = "success") -> None:
    st.session_state.setdefault("flash", []).append((kind, message))


def show_flash() -> None:
    for kind, message in st.session_state.pop("flash", []):
        {"success": st.success, "error": st.error, "info": st.info, "warning": st.warning}[kind](message)


def guarded(action: Callable[[], object], success: str = "") -> bool:
    """Run a store change from a callback; problems become a message instead of a crash."""
    try:
        action()
    except StoreError as error:
        flash(str(error), "error")
        return False
    if success:
        flash(success)
    return True


def attempts_left(kind: str, limit: int, window: int) -> Optional[int]:
    """Seconds to wait when this session failed too often, otherwise None."""
    now = time.time()
    recent = [moment for moment in st.session_state.get(f"attempts_{kind}", []) if now - moment < window]
    st.session_state[f"attempts_{kind}"] = recent
    return int(window - (now - recent[0])) + 1 if len(recent) >= limit else None


def record_attempt(kind: str) -> None:
    st.session_state.setdefault(f"attempts_{kind}", []).append(time.time())


def show_html(markup: str, height: int) -> None:
    if hasattr(st, "iframe"):
        st.iframe(markup, height=height)
    else:  # Streamlit versions before st.iframe
        import streamlit.components.v1 as components

        components.html(markup, height=height)


def dark_theme() -> bool:
    try:
        return getattr(st.context.theme, "type", None) == "dark"
    except Exception:  # noqa: BLE001 - older Streamlit versions
        return False


def viewer_timezone() -> Optional[str]:
    try:
        return st.context.timezone
    except Exception:  # noqa: BLE001
        return None


def local_clock(timestamp: Optional[float]) -> str:
    moment = export.local_time(timestamp, viewer_timezone())
    return moment.strftime("%H:%M") if moment else "—"


def app_url() -> str:
    """Where people open the app. Set QUIZ_PUBLIC_URL when the app runs behind a proxy with another address."""
    configured = os.environ.get("QUIZ_PUBLIC_URL", "").strip()
    if configured:
        return configured.rstrip("/") + "/"
    try:
        current = st.context.url or ""
    except Exception:  # noqa: BLE001
        current = ""
    if not current:
        return "http://localhost:8501/"
    parts = urlsplit(current)
    path = parts.path.replace("/~/+", "") or "/"   # Community Cloud serves the app from an inner path
    return urlunsplit((parts.scheme, parts.netloc, path if path.endswith("/") else path + "/", "", ""))


def short_url(url: str) -> str:
    """The address as people type it: https://quiz.example.com/ → quiz.example.com."""
    parts = urlsplit(url)
    return (parts.netloc + parts.path).rstrip("/") or url


def join_link(code: str) -> str:
    return f"{app_url()}?quiz={code}"


@st.cache_data(max_entries=64, show_spinner=False)
def qr_png(link: str) -> bytes:
    import segno

    buffer = io.BytesIO()
    segno.make(link, error="m").save(buffer, kind="png", scale=10, border=2, dark="#0f172a", light="#ffffff")
    return buffer.getvalue()


def lazy(producer: Callable[[], bytes]):
    """Build big downloads only when clicked (Streamlit versions that accept a callable), otherwise right away."""
    docs = (st.download_button.__doc__ or "").splitlines()
    data_types = next((line for line in docs if line.strip().startswith("data :")), "")
    return producer if "callable" in data_types else producer()


def show_chart(chart) -> None:
    if "width" in inspect.signature(st.altair_chart).parameters:
        st.altair_chart(chart, width="stretch")
    else:  # Streamlit versions before the width parameter
        st.altair_chart(chart, use_container_width=True)


def plain(value: str) -> str:
    return tx.escape_markdown(value)


def disclaimer() -> None:
    st.caption(
        f"⚠️ **Temporary by design.** Quizzes and answers live only in this app's memory, never in a database. "
        f"They are deleted {store().ttl_hours:g} hours after their last activity and can be lost earlier if the app "
        "restarts or goes to sleep. Create your quiz close to the event, keep its quiz file, and download the "
        "results when it ends."
    )


# ---------------------------------------------------------------------------- home

def home_page() -> None:
    st.title("🧠 Meeting Quiz")
    st.markdown("Quick quizzes for live events and online meetings. **Right answers and speed** decide the ranking, "
                "so you know exactly who deserves the prize.")
    show_flash()
    keep_selected("home_action", HOME_ACTIONS, HOME_ACTIONS[0])
    action = st.segmented_control("What do you want to do?", HOME_ACTIONS, key="home_action",
                                  label_visibility="collapsed")
    if action == "Join a quiz":
        join_box()
    elif action == "Create a quiz":
        create_box()
    else:
        manage_box()
    with st.expander("How it works"):
        st.markdown(
            """
**For organizers**
1. **Create** a quiz with single choice, multiple choice or open questions, required or optional.
2. **Share** the link, the QR code or the 6-digit code with your audience (for example on a Teams screen share).
3. **Open** it for a few minutes. People answer on their phone or laptop; the timer closes it for everyone.
4. **Reveal the winners** on screen and **download** every answer with names and emails.

**Privacy and security**
- Only the admin key opens the admin area. It's shown once and only a hash of it is kept.
- People answering never receive the right answers or other people's answers.
- Emails stay hidden on screen unless the admin chooses to show them, so screen sharing is safe.
- Everything is deleted automatically. Nothing is stored in a database.
"""
        )
    disclaimer()


def join_box() -> None:
    with st.form("join_form", border=True):
        st.markdown("#### Join a quiz")
        typed = st.text_input("Quiz code", max_chars=9, placeholder="123 456",
                              help="The 6-digit code the organizer shared.")
        submitted = st.form_submit_button("Join", type="primary", icon=":material/login:", width="stretch")
    if not submitted:
        return
    wait = attempts_left("join", *JOIN_LIMIT)
    code = tx.only_digits(typed)
    if wait:
        st.error(f"Too many attempts. Try again in {wait} seconds.")
    elif len(code) != 6 or store().get(code) is None:
        record_attempt("join")
        st.error("There's no quiz with this code. Check it with the organizer: quizzes are also deleted after a "
                 "while.")
    else:
        st.query_params["quiz"] = code
        st.rerun()


def start_admin(quiz: Quiz, key: str) -> None:
    st.session_state["admin_code"] = quiz.code
    st.session_state["new_key"] = key
    st.session_state["admin_view"] = VIEWS[0]
    record_attempt("create")


def create_box() -> None:
    wait = attempts_left("create", *CREATE_LIMIT)
    with st.form("create_form", border=True):
        st.markdown("#### Create a quiz")
        title = st.text_input("Quiz title", max_chars=MAX_TITLE, placeholder="e.g. Kick-off trivia")
        submitted = st.form_submit_button("Create quiz", type="primary", icon=":material/add:", width="stretch")
    if submitted:
        if wait:
            st.error(f"You created many quizzes in a short time. Try again in {wait // 60 + 1} minutes.")
        else:
            try:
                quiz, key = store().create(title=title)
            except StoreError as error:
                st.error(str(error))
            else:
                start_admin(quiz, key)
                st.rerun()

    left, right = st.columns(2, border=True)
    with left:
        st.markdown("**Try the sample quiz**")
        st.caption("Seven questions showing every question type, ready to open.")
        if st.button("Create sample quiz", icon=":material/auto_awesome:", width="stretch", disabled=bool(wait)):
            try:
                quiz, key = store().create(*sample_quiz())
            except StoreError as error:
                st.error(str(error))
            else:
                start_admin(quiz, key)
                st.rerun()
    with right:
        st.markdown("**Recreate from a quiz file**")
        upload = st.file_uploader("Quiz file", type=["json"], label_visibility="collapsed",
                                  help="Saved from the Build tab of a quiz. It has questions and settings, "
                                       "never responses.")
        if upload is not None and st.button("Create from file", icon=":material/upload_file:", width="stretch",
                                            disabled=bool(wait)):
            try:
                quiz, key = store().create(*quizfile.load(upload.getvalue()))
            except (quizfile.QuizFileError, StoreError) as error:
                st.error(str(error))
            else:
                start_admin(quiz, key)
                st.rerun()


def manage_box() -> None:
    with st.form("manage_form", border=True):
        st.markdown("#### Manage a quiz")
        code = st.text_input("Quiz code", max_chars=9, placeholder="123 456")
        key = st.text_input("Admin key", type="password", max_chars=40, placeholder="XXXX-XXXX-XXXX",
                            help="Shown once when the quiz was created.")
        submitted = st.form_submit_button("Open admin area", type="primary", icon=":material/lock_open:",
                                          width="stretch")
    if not submitted:
        return
    wait = attempts_left("admin", *ADMIN_LIMIT)
    if wait:
        st.error(f"Too many attempts. Try again in {wait} seconds.")
        return
    quiz = store().authenticate(code, key)
    if quiz is None:
        record_attempt("admin")
        st.error("The code or the admin key is wrong, or the quiz was deleted.")
        return
    st.session_state["admin_code"] = quiz.code
    st.session_state.pop("new_key", None)
    st.rerun()


# ---------------------------------------------------------------------------- admin

def sign_out() -> None:
    for key in ("admin_code", "new_key", "admin_view", "last_admin_view"):
        st.session_state.pop(key, None)


def admin_page(code: str) -> None:
    quiz = store().get(code)
    if quiz is None:
        sign_out()
        flash("This quiz no longer exists. Quizzes are deleted after a while without activity.", "warning")
        st.rerun()
    store().touch(code)
    now = store().now()
    status = quiz.status(now)

    head, actions = st.columns([5, 1], vertical_alignment="center")
    with head:
        st.markdown(f"## {plain(quiz.title)}")
        badges = st.container(horizontal=True, gap="small")
        with badges:
            st.badge(STATUS_LABELS[status], color=STATUS_COLORS[status])
            st.badge(f"Code {tx.format_code(quiz.code)}", icon=":material/tag:", color="gray")
            st.badge(tx.plural(len(quiz.questions), "question"), icon=":material/quiz:", color="gray")
    with actions:
        with st.container(horizontal_alignment="right"):
            st.button("Sign out", icon=":material/logout:", on_click=sign_out)
    show_flash()
    new_key_notice(quiz)

    keep_selected("admin_view", VIEWS, VIEWS[0])
    view = st.segmented_control("Section", VIEWS, key="admin_view", label_visibility="collapsed")
    if view == VIEWS[0]:
        build_view(quiz)
    elif view == VIEWS[1]:
        settings_view(quiz)
    elif view == VIEWS[2]:
        run_view(quiz)
    else:
        results_view(quiz)
    st.divider()
    disclaimer()


def new_key_notice(quiz: Quiz) -> None:
    key = st.session_state.get("new_key")
    if not key:
        return
    with st.container(border=True):
        st.markdown("#### 🔑 Save your admin key now")
        st.markdown("You need the **quiz code** and this **admin key** to manage the quiz from another tab or "
                    "device. The key can't be recovered. Don't share your screen while it's visible.")
        details = f"Quiz: {quiz.title}\nQuiz code: {tx.format_code(quiz.code)}\nAdmin key: {key}\n" \
                  f"Link for participants: {join_link(quiz.code)}\n"
        st.code(details, language=None)
        left, right = st.columns(2)
        left.download_button("Download as text file", details.encode("utf-8"), f"quiz-{quiz.code}-admin.txt",
                             "text/plain", icon=":material/download:", width="stretch")
        right.button("I saved my admin key", type="primary", icon=":material/check:", width="stretch",
                     on_click=lambda: st.session_state.pop("new_key", None))


# ---------------------------------------------------------------------------- build

def widget_key(*parts: object) -> str:
    """A widget key that includes the stored value it shows.

    Editor widgets always get their value from the store (``value=``), never through Session State, and the key
    changes whenever the stored value changes. A widget can then never report a stale or default value as an edit
    (Streamlit can do that when a rerun interrupts a run that set values through Session State), and changes made
    elsewhere (another tab, a duplicate or a reset) show up right away.
    """
    fingerprint = hashlib.sha1(repr(parts[-1]).encode("utf-8")).hexdigest()[:10]
    return ":".join(["ed", *map(str, parts[:-1]), fingerprint])


def build_view(quiz: Quiz) -> None:
    code = quiz.code
    title_key = widget_key(code, "title", quiz.title)
    st.text_input("Title", value=quiz.title, key=title_key, max_chars=MAX_TITLE,
                  on_change=lambda: guarded(lambda: store().update_details(code, title=st.session_state[title_key])))
    description_key = widget_key(code, "description", quiz.description)
    st.text_area("Description (optional)", value=quiz.description, key=description_key, max_chars=MAX_DESCRIPTION,
                 height=80, placeholder="Shown to people before they start.",
                 on_change=lambda: guarded(lambda: store().update_details(
                     code, description=st.session_state[description_key])))

    if quiz.locked:
        st.info("People already joined, so questions and options are locked. You can still fix **right answers** "
                "and **points**; results update right away. To change questions, remove all responses in **Run**.",
                icon=":material/lock:")

    for number, question in enumerate(quiz.questions, start=1):
        question_editor(quiz, question, number)

    if not quiz.locked:
        st.markdown("**Add a question**")
        buttons = st.container(horizontal=True)
        full = len(quiz.questions) >= MAX_QUESTIONS
        for kind, icon in ((SINGLE, ":material/radio_button_checked:"), (MULTIPLE, ":material/check_box:"),
                           (OPEN, ":material/short_text:")):
            buttons.button(KINDS[kind], icon=icon, key=f"add:{kind}", disabled=full, help=KIND_HELP[kind],
                           on_click=lambda k=kind: guarded(lambda: store().add_question(code, k)))

    issues, notes = quiz.issues(), quiz.notes()
    if issues:
        st.warning("**Before opening the quiz**\n\n" + "\n".join(f"- {issue}" for issue in issues),
                   icon=":material/warning:")
    elif quiz.questions:
        st.success(f"Ready to open: {tx.plural(len(quiz.questions), 'question')}, "
                   f"{tx.plural(quiz.max_points, 'point')} in total. Go to **Run** to share it.",
                   icon=":material/check_circle:")
    for note in notes:
        st.caption(f"ℹ️ {note}")

    st.markdown("**Quiz file**")
    st.caption("Save the questions, right answers and settings (never responses). If the app forgets your quiz, "
               "recreate it from this file in seconds.")
    st.download_button("Save quiz file", quizfile.dump(quiz), f"quiz-{slug(quiz.title)}.json", "application/json",
                       icon=":material/save:")


def slug(value: str) -> str:
    text = "-".join(tx.normalize(value).split())[:40]
    return text or "quiz"


def question_editor(quiz: Quiz, question: Question, number: int) -> None:
    code, qid, locked = quiz.code, question.id, quiz.locked

    def save(field: str, key: str) -> None:
        guarded(lambda: store().update_question(code, qid, **{field: st.session_state[key]}))

    def field(name: str, value: object) -> Dict[str, object]:
        """Keyword arguments shared by the widgets that edit one question field."""
        key = widget_key(code, qid, name, value)
        return {"key": key, "on_change": save, "args": (name, key)}

    with st.container(border=True):
        top = st.columns([2.2, 2, 1.4, 1.4, 2.4], vertical_alignment="bottom")
        top[0].markdown(f"**Question {number}**")
        top[1].selectbox("Type", list(KINDS), index=list(KINDS).index(question.kind), format_func=KINDS.get,
                         disabled=locked, **field("kind", question.kind))
        top[2].number_input("Points", min_value=0, max_value=MAX_POINTS, value=question.points, step=10,
                            help="0 makes it a poll or feedback question that doesn't count.",
                            **field("points", question.points))
        top[3].toggle("Required", value=question.required, disabled=locked, **field("required", question.required))
        with top[4]:
            tools = st.container(horizontal=True, gap="small", horizontal_alignment="right")
            last = len(quiz.questions)
            tools.button("", icon=":material/arrow_upward:", key=f"ed:{qid}:up", help="Move up",
                         disabled=locked or number == 1,
                         on_click=lambda: guarded(lambda: store().move_question(code, qid, -1)))
            tools.button("", icon=":material/arrow_downward:", key=f"ed:{qid}:down", help="Move down",
                         disabled=locked or number == last,
                         on_click=lambda: guarded(lambda: store().move_question(code, qid, 1)))
            tools.button("", icon=":material/content_copy:", key=f"ed:{qid}:copy", help="Duplicate",
                         disabled=locked or last >= MAX_QUESTIONS,
                         on_click=lambda: guarded(lambda: store().duplicate_question(code, qid)))
            tools.button("", icon=":material/delete:", key=f"ed:{qid}:delete", help="Delete question",
                         disabled=locked, on_click=lambda: guarded(lambda: store().remove_question(code, qid)))

        st.text_area("Question", value=question.text, max_chars=MAX_QUESTION, height=70, disabled=locked,
                     placeholder="Type your question", label_visibility="collapsed", **field("text", question.text))

        if question.is_choice:
            if question.scored:
                st.caption("Options · tick the right answer" + ("s" if question.kind == MULTIPLE else ""))
            else:
                st.caption("Options · this question has 0 points, so it works as a poll")
            for index, option in enumerate(question.options, start=1):
                right = option.id in question.correct
                right_key = widget_key(code, qid, "right", option.id, right)
                text_key = widget_key(code, qid, "option", option.id, option.text)
                row = st.columns([0.5, 9, 0.8], vertical_alignment="center")
                row[0].checkbox("Right answer", value=right, key=right_key, label_visibility="collapsed",
                                disabled=not question.scored, help="Right answer",
                                on_change=lambda oid=option.id, k=right_key: guarded(lambda: store().set_correct(
                                    code, qid, oid, bool(st.session_state[k]))))
                row[1].text_input(f"Option {index}", value=option.text, key=text_key, max_chars=MAX_OPTION,
                                  placeholder=f"Option {index}", label_visibility="collapsed", disabled=locked,
                                  on_change=lambda oid=option.id, k=text_key: guarded(lambda: store().set_option_text(
                                      code, qid, oid, st.session_state[k])))
                row[2].button("", icon=":material/close:", key=f"ed:{qid}:remove:{option.id}", help="Remove option",
                              disabled=locked or len(question.options) <= 2,
                              on_click=lambda oid=option.id: guarded(lambda: store().remove_option(code, qid, oid)))
            extras = st.container(horizontal=True, vertical_alignment="center")
            extras.button("Add option", icon=":material/add:", key=f"ed:{qid}:add_option",
                          disabled=locked or len(question.options) >= MAX_OPTIONS,
                          on_click=lambda: guarded(lambda: store().add_option(code, qid)))
            if question.kind == MULTIPLE and question.scored:
                extras.toggle("Partial credit", value=question.partial_credit,
                              help="Each right pick earns part of the points and each wrong pick takes part away. "
                                   "Off: all points only for exactly the right options.",
                              **field("partial_credit", question.partial_credit))
        elif question.scored:
            accepted = "\n".join(question.accepted)
            st.text_area("Accepted answers (one per line)", value=accepted, height=80, placeholder="e.g. Mars",
                         help="Case, accents and punctuation don't matter. Leave empty to review answers by hand "
                              "in Results.", **field("accepted", accepted))
            st.toggle("Accept small typos", value=question.tolerant,
                      help="'Jupyter' counts for 'Jupiter'. Answers with numbers always need to match exactly.",
                      **field("tolerant", question.tolerant))
        else:
            st.caption("Not scored: use it for opinions or feedback.")

        for issue in question.issues():
            st.caption(f":orange[⚠️ This question {issue}.]")


# ---------------------------------------------------------------------------- settings

def settings_view(quiz: Quiz) -> None:
    current = quiz.settings
    with st.form(f"settings:{quiz.code}", border=False):
        left, right = st.columns(2, gap="large")
        with left:
            st.markdown("#### Participants")
            ask_email = st.toggle("Ask for email", value=current.ask_email,
                                  help="Each email can answer only once. Without emails people only type a name.")
            domains = st.text_input("Only accept emails from these domains (optional)",
                                    value=", ".join(current.email_domains), placeholder="example.com, example.org",
                                    help="Leave empty to accept any email.")
            shuffle = st.toggle("Shuffle options for each person", value=current.shuffle_options,
                                help="Makes it harder to share answers like 'it's the second one' in the chat.")
            st.markdown("#### Time")
            minutes = st.number_input("Time limit (minutes)", min_value=0, max_value=MAX_DURATION_MINUTES,
                                      value=current.time_limit_minutes, step=1,
                                      help="Suggested when opening the quiz. 0 keeps it open until you close it.")
        with right:
            st.markdown("#### Ranking")
            ranking = st.radio("How people are ranked", list(RANKINGS), format_func=RANKINGS.get,
                               index=list(RANKINGS).index(current.ranking),
                               captions=[RANKING_HELP[k] for k in RANKINGS])
            window = st.number_input("Speed bonus window (seconds)", min_value=5, max_value=600,
                                     value=current.bonus_window_seconds, step=5,
                                     help="Only for the speed bonus: answers after this long earn no bonus.")
            st.markdown("#### Winners")
            winners = st.number_input("Number of winners", min_value=1, max_value=MAX_WINNERS, value=current.winners)
            min_score = st.slider("Minimum score to win (%)", 0, 100, value=current.min_score_percent, step=5)
            finished = st.toggle("Must finish the quiz to win", value=current.require_finished,
                                 help="People who didn't reach the last question before time ran out can't win.")
            not_eligible = st.text_area("Emails that can't win (optional)", value="\n".join(current.not_eligible),
                                        height=90, placeholder="organizer@example.com",
                                        help="For example organizers or people who already won a prize. "
                                             "They still appear in the ranking.")
        saved = st.form_submit_button("Save settings", type="primary", icon=":material/save:")
    if saved:
        settings = Settings(
            time_limit_minutes=int(minutes), ask_email=ask_email, email_domains=tx.clean_domains(domains),
            shuffle_options=shuffle, ranking=ranking, bonus_window_seconds=int(window), winners=int(winners),
            min_score_percent=int(min_score), require_finished=finished,
            not_eligible=[line for line in (tx.normalize_email(v) for v in not_eligible.replace(",", "\n")
                                            .splitlines()) if line],
        )
        if guarded(lambda: store().update_settings(quiz.code, settings), "Settings saved."):
            st.rerun()

    st.divider()
    st.markdown("#### Delete quiz")
    with st.popover("Delete this quiz now", icon=":material/delete_forever:"):
        st.write("Questions, participants and answers are erased right away. This can't be undone.")
        if st.button("Yes, delete everything", type="primary"):
            store().delete(quiz.code)
            sign_out()
            flash("Quiz deleted.", "info")
            st.rerun()


# ---------------------------------------------------------------------------- run

def join_card(quiz: Quiz) -> None:
    link = join_link(quiz.code)
    with st.container(border=True):
        text, image = st.columns([3, 2], vertical_alignment="center")
        with text:
            st.markdown(
                f'<div class="join-card"><p class="label">Go to</p><p class="url">{html.escape(short_url(app_url()))}</p>'
                f'<p class="label">and type the code</p><p class="code">{tx.format_code(quiz.code)}</p></div>',
                unsafe_allow_html=True,
            )
        with image:
            st.image(qr_png(link), width=210, caption="Scan to join")
    st.code(link, language=None)


def set_view(view: str) -> None:
    st.session_state["admin_view"] = view


def run_view(quiz: Quiz) -> None:
    code = quiz.code
    now = store().now()
    status = quiz.status(now)
    share, live = st.columns([3, 2], gap="large")
    with share:
        join_card(quiz)
    with live:
        if status == OPEN_STATUS:
            open_controls(quiz, now)
        else:
            closed_controls(quiz, status)
        participant_tools(quiz)
        live_panel(code, status)


def open_controls(quiz: Quiz, now: float) -> None:
    code = quiz.code
    if quiz.closes_at is not None:
        show_html(widgets.countdown_html(quiz.closes_at - now, quiz.closes_at - (quiz.opened_at or now), large=True,
                                         dark=dark_theme()), widgets.COUNTDOWN_LARGE_HEIGHT)
    else:
        st.info("Open with no time limit: close it when you're ready.", icon=":material/timer_off:")
    controls = st.container(horizontal=True)
    if quiz.closes_at is not None:
        controls.button("+1 min", icon=":material/more_time:", on_click=lambda: guarded(lambda: store().extend(code, 1)))
        controls.button("+5 min", icon=":material/more_time:", on_click=lambda: guarded(lambda: store().extend(code, 5)))
    controls.button("Close now", type="primary", icon=":material/stop_circle:",
                    on_click=lambda: guarded(lambda: store().close(code), "Quiz closed. Nobody can answer now."))


def closed_controls(quiz: Quiz, status: str) -> None:
    code = quiz.code
    issues = quiz.issues()
    if issues:
        st.warning("**Fix these in Build before opening**\n\n" + "\n".join(f"- {i}" for i in issues),
                   icon=":material/warning:")
    if status == CLOSED:
        st.info(f"Closed at {local_clock(quiz.closed_at or quiz.closes_at)}. Nobody can answer anymore.",
                icon=":material/lock_clock:")
    row = st.columns(2, vertical_alignment="bottom")
    minutes_key = widget_key(code, "open_minutes", quiz.settings.time_limit_minutes)
    row[0].number_input("Time limit (minutes)", min_value=0, max_value=MAX_DURATION_MINUTES,
                        value=quiz.settings.time_limit_minutes, key=minutes_key,
                        help="0 keeps it open until you close it.")
    label = "Open quiz" if status == DRAFT else "Reopen quiz"
    row[1].button(label, type="primary" if status == DRAFT else "secondary", icon=":material/play_arrow:",
                  disabled=bool(issues), width="stretch",
                  on_click=lambda: guarded(lambda: store().open(code, int(st.session_state[minutes_key])),
                                           "Quiz open! People can join now."))
    if status == CLOSED:
        actions = st.container(horizontal=True)
        actions.button("See results", type="primary", icon=":material/emoji_events:", on_click=set_view,
                       args=(VIEWS[3],))
        if quiz.participants:
            with actions.popover("Remove all responses", icon=":material/restart_alt:"):
                st.write(f"Erase {tx.plural(len(quiz.participants), 'participant')} and their answers, and bring "
                         "the quiz back to draft? Download the results first if you need them.")
                st.button("Yes, remove responses", type="primary", key=f"reset:{code}",
                          on_click=lambda: guarded(lambda: store().reset_responses(code), "Responses removed."))


def participant_tools(quiz: Quiz) -> None:
    code = quiz.code
    st.markdown("#### Participants")
    tools = st.container(horizontal=True, vertical_alignment="center")
    tools.toggle("Show emails", key="show_emails",
                 help="Emails stay hidden by default so you can share your screen safely.")
    if quiz.participants:
        with tools.popover("Remove someone", icon=":material/person_remove:"):
            people = sorted(quiz.participants.values(), key=lambda p: p.name.casefold())
            labels = {p.token: f"{p.name} · {p.email}" if p.email else p.name for p in people}
            st.caption("For example someone joined twice with different emails or isn't part of the event.")
            token = st.selectbox("Participant", list(labels), format_func=labels.get, index=None,
                                 placeholder="Choose someone", key=f"remove_choice:{code}")
            st.button("Remove", type="primary", disabled=token is None, key=f"remove:{code}",
                      on_click=lambda: guarded(lambda: store().remove_participant(code, token),
                                               "Participant removed."))


@st.fragment(run_every=3)
def live_panel(code: str, status: str) -> None:
    quiz = store().get(code)
    if quiz is None:
        return
    if quiz.status(store().now()) != status:
        st.rerun()
    participants = sorted(quiz.participants.values(), key=lambda p: p.joined_at)
    finished = sum(1 for p in participants if p.finished_at is not None)
    metrics = st.columns(3)
    metrics[0].metric("Joined", len(participants))
    metrics[1].metric("Answering", len(participants) - finished)
    metrics[2].metric("Finished", finished)
    if not participants:
        st.caption("People appear here as they join.")
        return
    show_emails = st.session_state.get("show_emails", False)
    frame = pd.DataFrame([{"Name": p.name, **({"Email": p.email} if show_emails else {}),
                           "Progress": progress(quiz, p), "Joined": local_clock(p.joined_at)}
                          for p in reversed(participants)])
    st.dataframe(frame, hide_index=True, width="stretch", height=min(360, 38 + 35 * len(frame)))


# ---------------------------------------------------------------------------- results

def results_view(quiz: Quiz) -> None:
    now = store().now()
    status = quiz.status(now)
    if not quiz.participants:
        st.info("No responses yet. Open the quiz in **Run** and share the code.", icon=":material/hourglass_empty:")
        return
    if status == OPEN_STATUS:
        st.warning("The quiz is still open, so the ranking can change. Close it in **Run** before revealing the "
                   "winners.", icon=":material/timer:")
    results = rank(quiz)
    keep_selected("results_view", RESULT_VIEWS, RESULT_VIEWS[0])
    top = st.columns([4, 1.4], vertical_alignment="center")
    with top[0]:
        view = st.segmented_control("Results", RESULT_VIEWS, key="results_view", label_visibility="collapsed")
    with top[1]:
        st.toggle("Show emails", key="show_emails",
                  help="Emails stay hidden by default so you can share your screen safely.")
    if view == "Ranking":
        ranking_section(quiz, results)
    elif view == "Winners":
        winners_section(quiz, results)
    elif view == "Questions":
        questions_section(results)
    elif view == "Review answers":
        review_section(quiz, results)
    else:
        downloads_section(quiz, results, now)


def ranking_section(quiz: Quiz, results: Results) -> None:
    metrics = st.columns(4)
    metrics[0].metric("Participants", results.participants)
    metrics[1].metric("Finished", results.finished)
    metrics[2].metric("Average score", f"{results.average_percent:.0f}%" if results.average_percent is not None
                      else "—")
    metrics[3].metric("Winners", f"{len(results.winners)} of {quiz.settings.winners}")
    show_emails = st.session_state.get("show_emails", False)
    frame = export.ranking_frame(results, viewer_timezone(), include_email=show_emails)
    frame = frame.drop(columns=["Joined at", "Finished at"])
    st.dataframe(
        frame, hide_index=True, width="stretch", height=min(560, 38 + 35 * len(frame)),
        column_config={
            "Rank": st.column_config.NumberColumn(width="small"),
            "Winner": st.column_config.TextColumn("🏆", width="small"),
            "Score %": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
            "Time (s)": st.column_config.NumberColumn(format="%.1f"),
        },
    )
    st.caption("Winning rules: " + " · ".join(describe_rules(quiz.settings, quiz.max_points)) +
               ". Change them in **Settings**; the ranking updates right away.")


def winners_section(quiz: Quiz, results: Results) -> None:
    st.caption("Share your screen and reveal the winners one by one, from the last place to the first. Only names, "
               "points and times are shown.")
    show_html(widgets.winners_html(quiz, results), widgets.winners_height(len(results.winners)))
    if len(results.winners) < quiz.settings.winners:
        missing = quiz.settings.winners - len(results.winners)
        st.caption(f"{tx.plural(missing, 'prize')} left without a winner: not enough people meet the winning rules.")


def questions_section(results: Results) -> None:
    import altair as alt

    highlight = st.toggle("Highlight right answers", value=False,
                          help="Off by default, in case you share your screen and want to reuse the quiz.")
    for stats in results.questions:
        question = stats.question
        with st.container(border=True):
            st.markdown(f"**{stats.number}. {plain(question.text)}**")
            facts = [KINDS[question.kind], tx.plural(question.points, "point") if question.scored else "not scored",
                     f"{stats.answered} of {stats.seen} answered"]
            if stats.percent_right is not None:
                facts.append(f"**{stats.percent_right:.0f}% right**")
            if stats.average_seconds is not None:
                facts.append(f"{tx.format_seconds(stats.average_seconds)} on average")
            st.caption(" · ".join(facts))
            if question.is_choice and stats.answered:
                frame = pd.DataFrame([{"Option": f"{index}. {o.text}", "People": o.count,
                                       "Answer": "Right" if (highlight and o.right) else "Other"}
                                      for index, o in enumerate(stats.options, start=1)])
                chart = alt.Chart(frame).mark_bar(cornerRadiusEnd=4).encode(
                    x=alt.X("People:Q", title="People", axis=alt.Axis(tickMinStep=1, format="d")),
                    y=alt.Y("Option:N", sort=None, title=None, axis=alt.Axis(labelLimit=360, labelOverlap=False)),
                    color=alt.Color("Answer:N", scale=alt.Scale(domain=["Right", "Other"],
                                                                 range=["#16a34a", "#94a3b8"]), legend=None),
                    tooltip=["Option", "People"],
                ).properties(height=40 * len(frame) + 30)
                show_chart(chart)
            elif stats.open_groups:
                frame = pd.DataFrame([{"Answer": g.text, "People": g.count,
                                       **({"Right": g.right} if highlight and question.scored else {})}
                                      for g in stats.open_groups])
                st.dataframe(frame, hide_index=True, width="stretch", height=min(300, 38 + 35 * len(frame)))


def review_section(quiz: Quiz, results: Results) -> None:
    reviewable = [s for s in results.questions if s.question.kind == OPEN and s.question.scored]
    if not reviewable:
        st.info("There are no scored open questions to review.", icon=":material/rate_review:")
        return
    st.caption("Answers are checked automatically against the accepted answers. Tick or untick **Right** to "
               "decide yourself; the ranking updates right away.")
    for stats in reviewable:
        question = stats.question
        st.markdown(f"**{stats.number}. {plain(question.text)}**")
        if not stats.open_groups:
            st.caption("No answers yet.")
            continue
        groups = stats.open_groups
        editor = f"review:{quiz.code}:{question.id}:{quiz.revision}"
        frame = pd.DataFrame([{"Answer": g.text, "People": g.count, "Automatic": g.automatic, "Right": g.right}
                              for g in groups])

        def apply(editor: str = editor, groups: List = groups, qid: str = question.id) -> None:
            for row, change in st.session_state[editor].get("edited_rows", {}).items():
                if "Right" not in change:
                    continue
                group = groups[int(row)]
                value = bool(change["Right"])
                guarded(lambda: store().set_review(quiz.code, qid, group.key, None if value == group.automatic
                                                   else value))

        st.data_editor(frame, key=editor, hide_index=True, width="stretch", disabled=["Answer", "People", "Automatic"],
                       height=min(420, 38 + 35 * len(frame)), on_change=apply,
                       column_config={"Automatic": st.column_config.CheckboxColumn(help="Matches an accepted answer"),
                                      "Right": st.column_config.CheckboxColumn(help="Counts as right")})


def downloads_section(quiz: Quiz, results: Results, now: float) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    name = slug(quiz.title)
    timezone_name = viewer_timezone()
    st.markdown("Download the results before the quiz is deleted. Files include **names, emails and every answer**: "
                "store them according to your privacy rules.")
    columns = st.columns(3)
    columns[0].download_button(
        "Full results (Excel)", lazy(lambda: export.results_workbook(quiz, results, now, timezone_name)),
        f"{name}-results-{stamp}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary", icon=":material/table_view:", width="stretch")
    columns[1].download_button(
        "Ranking (CSV)", export.to_csv(export.ranking_frame(results, timezone_name)), f"{name}-ranking-{stamp}.csv",
        "text/csv", icon=":material/download:", width="stretch")
    columns[2].download_button("Quiz file", quizfile.dump(quiz), f"quiz-{name}.json", "application/json",
                               icon=":material/save:", width="stretch")
    st.caption("The Excel file has four sheets: Summary, Ranking, Answers (one row per person) and Questions.")


# ---------------------------------------------------------------------------- participants

def leave_quiz() -> None:
    st.query_params.clear()


def participant_page(code: str) -> None:
    quiz = store().get(code)
    if quiz is None:
        st.title("🧠 Meeting Quiz")
        st.error("There's no quiz with this code. It may have ended: quizzes are deleted after a while.",
                 icon=":material/search_off:")
        st.button("Go to the start page", on_click=leave_quiz, icon=":material/home:")
        return

    st.markdown(f"## {plain(quiz.title)}")
    token = st.query_params.get("me", "")
    participant = store().participant(code, token) if token else None
    now = store().now()
    status = quiz.status(now)

    if participant is None:
        if quiz.description:
            st.markdown(plain(quiz.description))
        if status == DRAFT:
            waiting_room(code, status)
        elif status == CLOSED:
            st.info("This quiz is closed. Thanks for your interest!", icon=":material/lock_clock:")
        else:
            join_form(quiz, now)
        return

    if participant.finished_at is not None:
        finished_screen(quiz, participant)
    elif status != OPEN_STATUS:
        answered = len(participant.answers)
        st.info(f"⏰ Time's up! Your {tx.plural(answered, 'answer')} {'was' if answered == 1 else 'were'} saved. "
                "The organizer will share the results.")
    else:
        question_screen(quiz, participant, now)


@st.fragment(run_every=5)
def waiting_room(code: str, status: str) -> None:
    quiz = store().get(code)
    if quiz is None or quiz.status(store().now()) != status:
        st.rerun()
    st.info("⏳ The quiz hasn't started yet. Keep this page open: it updates by itself.")


def join_form(quiz: Quiz, now: float) -> None:
    facts = [tx.plural(len(quiz.questions), "question")]
    remaining = quiz.remaining(now)
    if remaining is not None:
        facts.append(f"{tx.format_clock(remaining)} left")
    facts.append("speed counts" if quiz.settings.ranking == SPEED_BONUS else "faster answers break ties")
    st.caption(" · ".join(facts))
    with st.form("participant_join", border=True):
        name = st.text_input("Your name", max_chars=MAX_NAME, autocomplete="name")
        email = st.text_input("Your email", max_chars=MAX_EMAIL, autocomplete="email") \
            if quiz.settings.ask_email else ""
        st.caption("The organizer sees your name" + (", email" if quiz.settings.ask_email else "") +
                   " and answers to run the quiz and hand out prizes. Everything is deleted automatically.")
        start = st.form_submit_button("Start quiz", type="primary", icon=":material/play_arrow:", width="stretch")
    st.caption("You answer one question at a time and can't go back. Time counts from when each question appears.")
    if start:
        try:
            participant = store().join(quiz.code, name, email)
        except StoreError as error:
            st.error(str(error))
        else:
            st.query_params["me"] = participant.token
            st.rerun()


def option_order(quiz: Quiz, participant: Participant, question: Question) -> List:
    options = list(question.options)
    if quiz.settings.shuffle_options:
        random.Random(f"{participant.token}:{question.id}").shuffle(options)
    return options


def question_screen(quiz: Quiz, participant: Participant, now: float) -> None:
    question = QuizStore.next_question(quiz, participant)
    if question is None:
        return
    try:
        store().show(quiz.code, participant.token, question.id)
    except StoreError as error:
        st.info(str(error))
        return
    number = quiz.questions.index(question) + 1
    total = len(quiz.questions)
    st.progress((number - 1) / total, text=f"Question {number} of {total}")
    if quiz.closes_at is not None:
        show_html(widgets.countdown_html(quiz.closes_at - now, quiz.closes_at - (quiz.opened_at or now),
                                         dark=dark_theme()), widgets.COUNTDOWN_HEIGHT)

    form_key = f"answer:{participant.token}:{question.id}"
    with st.form(form_key, border=True):
        st.markdown(f'<p class="quiz-question">{html.escape(question.text).replace(chr(10), "<br>")}</p>',
                    unsafe_allow_html=True)
        hints = []
        if question.kind == MULTIPLE:
            hints.append("Select all that apply")
        if not question.required:
            hints.append("Optional")
        if hints:
            st.caption(" · ".join(hints))
        picked: List[str] = []
        typed = ""
        options = option_order(quiz, participant, question)
        if question.kind == SINGLE:
            labels = {option.id: plain(option.text) for option in options}
            choice = st.radio("Your answer", list(labels), format_func=labels.get, index=None,
                              label_visibility="collapsed", key=f"{form_key}:radio")
            picked = [choice] if choice else []
        elif question.kind == MULTIPLE:
            for option in options:
                if st.checkbox(plain(option.text), key=f"{form_key}:{option.id}"):
                    picked.append(option.id)
        else:
            typed = st.text_area("Your answer", max_chars=MAX_ANSWER, label_visibility="collapsed",
                                 placeholder="Type your answer", key=f"{form_key}:text")
        last = number == total
        submit = st.form_submit_button("Finish" if last else "Next", type="primary",
                                       icon=":material/check:" if last else ":material/arrow_forward:",
                                       width="stretch")
        skip = False if question.required else st.form_submit_button("Skip", width="stretch")
    if submit or skip:
        try:
            store().answer(quiz.code, participant.token, question.id, choice=picked, text=typed, skipped=skip)
        except StoreError as error:
            st.error(str(error))
        else:
            st.rerun()


def finished_screen(quiz: Quiz, participant: Participant) -> None:
    seconds = sum(answer.seconds for answer in participant.answers.values())
    celebrated = f"celebrated:{participant.token}"
    if not st.session_state.get(celebrated):
        st.session_state[celebrated] = True
        st.balloons()
    st.success(f"🎉 All done, {plain(participant.name)}! Your answers were saved.")
    st.markdown(f"You answered {tx.plural(len(quiz.questions), 'question')} in **{tx.format_seconds(seconds)}**. "
                "Stay tuned: the organizer will reveal the winners.")


# ---------------------------------------------------------------------------- entry point

def main() -> None:
    code = tx.only_digits(st.query_params.get("quiz", ""))[:6]
    admin_code = st.session_state.get("admin_code")
    page = "participant" if code else ("admin" if admin_code else "home")
    st.set_page_config(page_title=APP_NAME, page_icon="🧠", layout="wide" if page == "admin" else "centered")
    st.html(CSS)
    if page == "participant":
        participant_page(code)
    elif page == "admin":
        admin_page(admin_code)
    else:
        home_page()


main()

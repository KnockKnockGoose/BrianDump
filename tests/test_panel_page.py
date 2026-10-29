"""Smoke test of the panel page in Chromium (Playwright) against a real sandbox server process.

    py -m pytest tests/test_panel_page.py -q          # needs: pip install playwright && py -m playwright install chromium

A throwaway world (conftest's `home`, sandbox on): one small transcript in a fake ~/.claude, agenda.md and questions.md, two
dump folders written by hand (a ready one with the sample cards, a failed one), and a fake hold-to-talk service that keeps
ptt_status.json fresh. `py -m briandump.server --port 0` serves it (a free port; config: mock processor, no chime); the test
loads the page, clicks through Panel | Dumps | Voice, presses Dump, answers questions, ticks the agenda, sends an action, and
asserts the files those clicks must produce, with zero console errors, page errors, failed requests or outside requests.

Screenshots go to $BRIANDUMP_SHOTS when it is set (else the test's temp folder): panel-640.png, panel-420.png, dumps-640.png.
Skipped when Playwright or its Chromium is not installed (CI ignores this file).
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api", reason="pip install playwright (and py -m playwright install chromium)")

from briandump import server, voice  # noqa: E402

pytestmark = pytest.mark.slow
REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "briandump" / "fixtures"
HOST = "testhost"
READY, FAILED = "20261018-093000-a1b2", "20261018-101500-c3d4"
SESSION_ID = "11111111-2222-3333-4444-555555555555"


def iso(age=0.0):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - age))


def wait_for(cond, timeout=10.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = cond()
        if got:
            return got
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


class FakePTT:
    """Keeps ptt_status.json fresh, as the hold-to-talk service does every 2 s (every 0.25 s while recording)."""

    def __init__(self, home):
        self.home, self.stop, self.recording = home, threading.Event(), None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def status(self):
        rec = self.recording
        return {"pid": 4242, "ts": iso(), "ready": True, "key": "f9", "dump_key": "f2", "device": "Test microphone",
                "model": "small", "recording": bool(rec), "mode": "dump" if rec else None, "started": iso(5) if rec else None,
                "level_db": -18.0 if rec else None, "peak_db": -6.0 if rec else None, "transcribing": None, "last_error": None}

    def run(self):
        while not self.stop.is_set():
            try:
                server.atomic_write(self.home.ptt_status, json.dumps(self.status()))
            except (OSError, server.Busy):
                pass
            self.stop.wait(0.3)


def build_world(home, tmp_path):
    """Everything the page shows, written the way the real writers write it."""
    reports = tmp_path / "reports-root"
    write_reports(home, reports)
    home.config_file.write_text(json.dumps({"dump": {"processor": "mock", "claude_cmd": "briandump-test-no-such-command"},
                                            "voice": {"mode": "panel"}, "needs_you_chime": False,
                                            "reports": {"index": str(reports / "index.json"), "roots": [str(reports)],
                                                        "discover": "^Prepared for "}}), encoding="utf-8")
    project = tmp_path / "garden-shed"
    project.mkdir()
    lines = [
        {"type": "user", "message": {"content": "Plan the garden shed with me"}, "timestamp": iso(60), "cwd": str(project)},
        {"type": "assistant", "timestamp": iso(55), "cwd": str(project),
         "message": {"model": "claude-test",
                     "content": [{"type": "text", "text": "Here is a first plan for the shed."},
                                 {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": str(project / "plan.md")}}],
                     "usage": {"input_tokens": 900, "cache_read_input_tokens": 30000, "output_tokens": 200}}},
        {"type": "user", "timestamp": iso(50), "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {"type": "assistant", "timestamp": iso(45), "cwd": str(project),
         "message": {"model": "claude-test",
                     "content": [{"type": "text", "text": "Which base do you prefer for the shed, slab or gravel?"}],
                     "usage": {"input_tokens": 1200, "cache_read_input_tokens": 41000, "output_tokens": 300}}},
        {"type": "system", "subtype": "turn_duration", "timestamp": iso(44)},
    ]
    folder = home.projects / "C--work-garden-shed"
    folder.mkdir(parents=True)
    with open(folder / f"{SESSION_ID}.jsonl", "w", encoding="utf-8", newline="\n") as f:
        f.writelines(json.dumps(e) + "\n" for e in lines)
    # the session is running (its registry record), so an answer to its question goes back to it; you are at the keyboard
    # (the sandbox console's keyboard_busy flag), so the voice router waits and types nothing during the test
    (home.sessions / "4243.json").write_text(json.dumps({"pid": 4243, "sessionId": SESSION_ID, "cwd": str(project), "name": "Main",
                                                         "entrypoint": "cli", "status": "idle", "startedAt": 1_792_000_000_000}),
                                             encoding="utf-8")
    (home.state_dir / "keyboard_busy").write_text("", encoding="utf-8")
    home.agenda_file.write_text("# Agenda\n\n- [ ] Peg the corners of the shed base\n- [ ] Order roofing felt\n", encoding="utf-8")
    home.questions_file.write_text("# Questions\n\n- [ ] How much does the leaning fence take off the usable width?\n",
                                   encoding="utf-8")
    for did, status in ((READY, "ready"), (FAILED, "error")):
        d = home.dumps_dir / did
        d.mkdir(parents=True)
        meta = {"id": did, "createdAt": f"2026-10-18T{did[9:11]}:{did[11:13]}:00+00:00", "updatedAt": iso(), "durationSec": 94.0,
                "status": status, "error": "claude -p timed out" if status == "error" else None, "attempts": 2, "timings": {},
                "words": 312, "source": "ptt", "label": None, "host": HOST, "processor": "mock", "model": None, "cost_usd": None,
                "report": None, "sent": {}, "agenda_added": [], "questions_added": []}
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        if status == "ready":
            shutil.copyfile(FIXTURES / "sample-transcript.txt", d / "transcript.txt")
            shutil.copyfile(FIXTURES / "sample-cards.json", d / "cards.json")


def write_reports(home, root):
    """Three reports for the Reports tab: one found by its byline, one named in an index, one written by a brain dump."""
    docs = root / "garden-shed" / "docs"
    docs.mkdir(parents=True)
    (docs / "2026-10-19-shed-plan.md").write_text(
        "# The shed plan\n\nPrepared for Sam · 2026-10-19\n\n## Materials\n\n| Part | Qty | Note |\n|---|--:|---|\n"
        "| Post | 4 | pressure-treated, 2.4 m |\n| Roof felt | 1 roll | `type B` |\n\nSee [the budget](budget.md) and "
        "<script>alert(1)</script>.\n\n- [x] posts ordered\n- [ ] felt\n", encoding="utf-8")
    (docs / "budget.md").write_text("# Budget\n\n**Total:** about 640 for timber and felt.\n", encoding="utf-8")
    (root / "index.json").write_text(json.dumps([{"path": "garden-shed/docs/budget.md", "title": "Shed budget",
                                                  "date": "2026-10-18", "by": "Main"}]), encoding="utf-8")
    home.reports_dir.mkdir(parents=True, exist_ok=True)
    (home.reports_dir / f"{READY}-garden-shed.md").write_text("# Brain dump · 2026-10-18 09:30 (2 min)\n\n## Summary\n\nThe shed.\n",
                                                              encoding="utf-8")


def start_server(home):
    """`py -m briandump.server --port 0` in the sandbox; -> (process, port, log path) once it has announced itself."""
    log = home.state_dir / "server-under-test.log"
    out = open(log, "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "briandump.server", "--port", "0"], cwd=REPO, stdout=out, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL)
    out.close()
    try:
        def announced():
            if proc.poll() is not None:
                raise AssertionError(f"server exited with {proc.returncode}:\n{log.read_text(encoding='utf-8')}")
            info = server.read_shared_json(home.state / "server.json")
            return info if isinstance(info, dict) and info.get("pid") == proc.pid else None
        info = wait_for(announced, 30, "the server to announce its port")
    except BaseException:
        proc.kill()
        proc.wait(timeout=10)
        raise
    assert info["port"] not in (8765, 8770) and info["sandbox"] is True
    return proc, info["port"], log


def inbox_texts(home):
    """The words of each panel line (a routing first line, voice.META, is left out: inbox_meta has it)."""
    return [voice.split_meta(p.read_text(encoding="utf-8"))[1] for p in sorted(home.inbox.glob(f"*-panel-{HOST}.txt"))]


def inbox_meta(home):
    return [voice.split_meta(p.read_text(encoding="utf-8"))[0] for p in sorted(home.inbox.glob(f"*-panel-{HOST}.txt"))]


def test_panel_page_smoke(home, tmp_path):
    shots = Path(os.environ.get("BRIANDUMP_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    build_world(home, tmp_path)
    ptt = FakePTT(home)
    ptt.thread.start()
    proc, port, log = start_server(home)
    base = f"http://127.0.0.1:{port}"
    problems, live = [], [True]                              # live: stop counting once the browser is being closed

    def problem(text):
        if live[0]:
            problems.append(text)
    try:
        with sync_api.sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True)
            except Exception as e:  # noqa: BLE001 - no browser on this machine: nothing to test here
                pytest.skip(f"Chromium cannot launch ({str(e).splitlines()[0]}); run: py -m playwright install chromium")
            try:
                page = browser.new_context(viewport={"width": 640, "height": 1000}, locale="en-US").new_page()
                page.set_default_timeout(10_000)
                page.on("console", lambda m: m.type == "error" and problem(f"console: {m.text}"))
                page.on("pageerror", lambda e: problem(f"page error: {e}"))
                page.on("requestfailed", lambda r: problem(f"request failed: {r.method} {r.url} {r.failure}"))
                page.on("response", lambda r: r.status >= 400 and problem(f"HTTP {r.status} {r.request.method} {r.url}"))

                def outside(route):
                    if route.request.url.startswith(base) or route.request.url.startswith("data:"):
                        return route.continue_()
                    problem(f"outside request: {route.request.url}")
                    return route.abort()
                page.route("**/*", outside)

                page.goto(base + "/")
                assert page.title() == "BrianDump · SANDBOX"
                page.wait_for_function("document.querySelector('#activity').textContent !== 'Connecting…'")
                page.wait_for_selector("#agenda .ag")
                page.wait_for_function("document.querySelectorAll('#questions .q').length >= 2")
                page.wait_for_selector("#dumpbtn:not([disabled])")
                assert page.inner_text("#activity") == "Waiting for you"
                assert "Dump" in page.inner_text("#dumpbtn")
                assert "Mock cards: no model call" in page.get_attribute("#dumpbtn", "title")         # dump.processor = mock here
                assert page.inner_text("#dnote").startswith("Cards come from the mock processor")
                assert "about one Claude message" in server.CONF.panel_html.read_text(encoding="utf-8")   # the claude processor's wording
                page.wait_for_function("document.querySelector('#dbadge').textContent === '1'")     # one unseen ready dump
                page.screenshot(path=str(shots / "panel-640.png"))
                page.set_viewport_size({"width": 420, "height": 1000})
                page.wait_for_timeout(300)
                assert page.evaluate("document.documentElement.scrollWidth <= 420"), "the page scrolls sideways at 420 px"
                page.screenshot(path=str(shots / "panel-420.png"))
                page.set_viewport_size({"width": 640, "height": 1000})

                # Dump: the button writes the command file the hold-to-talk service polls
                page.click("#dumpbtn")
                cmd = wait_for(lambda: server.read_shared_json(home.ptt_command), 5, "ptt_command.json")
                assert cmd["cmd"] == "dump_toggle" and cmd["nonce"]
                ptt.recording = True                                       # the service reports a dump recording
                page.wait_for_function("document.querySelector('#dumptxt').textContent.startsWith('Recording · 00:0')")
                assert page.inner_text("#dumpbtn") == "■ Stop" and page.is_visible("#level")
                ptt.recording = None
                page.wait_for_function("document.querySelector('#dumptxt').textContent.startsWith('press F2')")

                # a question Claude asked, answered by typing: the inbox gets the panel line
                card = page.locator("#questions .q", has_text="Which base do you prefer")
                assert "claude asked · main" in card.inner_text().lower()           # the card names the session that asked
                card.locator("input").fill("Gravel, it is a weekend job.")
                card.locator("input").press("Enter")
                wait_for(lambda: inbox_texts(home), 5, "the answer in the inbox")
                assert inbox_texts(home) == ["[panel answer to: Which base do you prefer for the shed, slab or gravel?] "
                                             "Gravel, it is a weekend job."]
                assert inbox_meta(home) == [{"session": SESSION_ID}]             # it goes back to the session that asked

                # a question from questions.md: answered, then ticked in the file
                card = page.locator("#questions .q", has_text="leaning fence")
                assert "from your brain dump" in card.inner_text().lower()          # (the label is uppercased by CSS)
                card.locator("input").fill("About 20 cm.")
                card.locator("input").press("Enter")
                wait_for(lambda: "- [x] How much" in home.questions_file.read_text(encoding="utf-8"), 5, "questions.md ticked")

                # the agenda: a tick edits agenda.md
                page.locator("#agenda .ag", has_text="Order roofing felt").click()
                wait_for(lambda: "- [x] Order roofing felt" in home.agenda_file.read_text(encoding="utf-8"), 5, "agenda.md ticked")

                # Dumps tab: the list, the detail, Send to Claude with an edited text, Add to agenda
                page.click("#tabs button[data-tab=dumps]")
                page.wait_for_function("document.querySelectorAll('#dumplist .dl').length === 2")
                assert "claude -p timed out" in page.inner_text("#dumplist .dl.error")
                page.click("#dumplist .dl.ready")
                page.wait_for_selector("#dumpdetail h3")
                heads = page.locator("#dumpdetail h3").all_inner_texts()
                assert [h.upper() for h in heads] == ["SUMMARY", "THEMES", "PROPOSED ACTIONS", "TO-DOS", "QUESTIONS"]
                assert page.is_hidden("#dbadge")                         # seen now
                page.click("#dumpdetail button[data-open=a1]")
                box = page.locator("#sendbox")
                assert box.input_value().startswith("Summarise the permit rules for small sheds: Write a short note")
                box.fill("Summarise the permit rules for small sheds in five lines.")
                page.click("#dumpdetail button[data-send=a1]")
                page.wait_for_selector("#dumpdetail .act .sent")
                assert inbox_texts(home)[-1] == "Summarise the permit rules for small sheds in five lines."
                page.click("#dumpdetail button[data-todo='0']")
                page.wait_for_selector("#dumpdetail .todo .ok")
                assert "Measure the shed space with the long tape" in home.agenda_file.read_text(encoding="utf-8")
                q = page.locator("#dumpdetail .q", has_text="lumber yard quote")
                q.locator("input").fill("Not yet, calling tomorrow.")
                q.locator("input").press("Enter")
                wait_for(lambda: len(inbox_texts(home)) == 4, 5, "the dump question's answer")
                assert inbox_texts(home)[-1].startswith("[BrianDump question: What did the lumber yard quote for the frame")
                page.locator("#dumpdetail details summary").click()
                assert "the shed again" in page.inner_text("#dumpdetail pre")
                page.click("#dumplist .dl.error")
                page.wait_for_selector("#dumpdetail button[data-retry]")
                page.click("#dumplist .dl.ready")
                page.wait_for_selector("#dumpdetail .act .sent")
                page.screenshot(path=str(shots / "dumps-640.png"))

                # Voice tab: the router's view (panel mode; you are at the keyboard, so every line waits)
                page.click("#tabs button[data-tab=voice]")
                page.wait_for_selector("#voicestrip .vh")
                assert "panel mode" in page.inner_text("#vsum")

                # Reports tab: a report found by its byline, one from the index and a brain dump's, newest first, unread
                page.wait_for_function("document.querySelector('#rbadge').textContent === '3'")
                page.click("#tabs button[data-tab=reports]")
                page.wait_for_function("document.querySelectorAll('#replist .rl').length === 3")
                assert page.locator("#replist .rl .rtitle").all_inner_texts()[0] == "The shed plan"
                assert page.locator("#replist .rl.unread").count() == 3 and page.is_hidden("#rnote")
                page.screenshot(path=str(shots / "reports-640.png"))
                page.locator("#replist .rl", has_text="The shed plan").click()
                page.wait_for_selector("#repdoc h1")
                assert page.inner_text("#repdoc h1") == "The shed plan" and page.locator("#repdoc table th").count() == 3
                assert page.locator("#repdoc script").count() == 0 and "<script>alert(1)</script>" in page.inner_text("#repdoc")
                wait_for(lambda: home.reports_read.exists(), 5, "the read mark")                      # opening it marks it read
                page.wait_for_function("document.querySelector('#rbadge').textContent === '2'")
                dark = page.evaluate("document.querySelector('#repdoc').classList.contains('dark')")
                page.click("#reptheme")                                                                 # light <-> dark paper
                assert page.evaluate("document.querySelector('#repdoc').classList.contains('dark')") is not dark
                page.screenshot(path=str(shots / "report-640.png"))
                page.set_viewport_size({"width": 420, "height": 1000})
                page.wait_for_timeout(300)
                assert page.evaluate("document.documentElement.scrollWidth <= 420"), "a report scrolls sideways at 420 px"
                page.set_viewport_size({"width": 640, "height": 1000})
                # Save as PDF: the browser's own print dialog, with the report's title as the file name
                page.evaluate("window.print = () => { window.__printed = document.title; }")
                page.click("#reppdf")
                assert page.evaluate("window.__printed") == "The shed plan" and page.title() == "BrianDump · SANDBOX"
                page.emulate_media(media="print")                                   # the print is the report alone
                assert page.is_hidden("#status") and page.is_hidden("#repbar") and page.is_hidden("#tabs")
                assert page.is_visible("#repdoc h1")
                page.pdf(path=str(shots / "report.pdf"))
                page.emulate_media(media="screen")
                # a link to another listed report opens it on the panel; Open file is only recorded in the sandbox
                page.click("#repdoc a[data-report]")
                page.wait_for_function("document.querySelector('#repdoc h1').textContent === 'Budget'")
                page.click("#repopen")
                page.wait_for_function("document.querySelector('#repmsg').textContent.startsWith('sandbox')")
                assert [Path(e["path"]).name for e in home.side_effect_log() if e["kind"] == "open_report"] == ["budget.md"]
                page.keyboard.press("Escape")                                                           # back to the list
                page.wait_for_selector("#replist .rl")
                assert page.locator("#replist .rl.unread").count() == 1                                 # the dump's report is left
                page.click("#rreadall")
                page.wait_for_function("document.querySelectorAll('#replist .rl.unread').length === 0")
                assert page.is_hidden("#rbadge")
                page.click("#tabs button[data-tab=panel]")
                page.wait_for_selector("#questions .q")
                page.wait_for_timeout(1500)                               # a few more polls with everything on screen
            finally:
                live[0] = False
                browser.close()
    finally:
        ptt.stop.set()
        proc.kill()
        proc.wait(timeout=10)
    assert problems == [], "\n".join(problems) + "\n--- server log ---\n" + log.read_text(encoding="utf-8")[-3000:]
    assert not [e for e in home.side_effect_log() if e["kind"] in ("type_text", "launch_main", "window")]


def test_mic_answer_on_the_page(home, tmp_path):
    """🎤 Answer: the button arms its card's question; the strip says so on every tab, with a countdown; the card is
    outlined; Esc cancels; a dump deck's question arms too, and its button then cancels it. Arming never types anything."""
    shots = Path(os.environ.get("BRIANDUMP_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    build_world(home, tmp_path)
    ptt = FakePTT(home)
    ptt.thread.start()
    proc, port, log = start_server(home)
    base = f"http://127.0.0.1:{port}"
    problems = []
    try:
        with sync_api.sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True)
            except Exception as e:  # noqa: BLE001 - no browser on this machine: nothing to test here
                pytest.skip(f"Chromium cannot launch ({str(e).splitlines()[0]}); run: py -m playwright install chromium")
            try:
                page = browser.new_context(viewport={"width": 640, "height": 1000}, locale="en-US").new_page()
                page.set_default_timeout(10_000)
                page.on("pageerror", lambda e: problems.append(f"page error: {e}"))
                page.on("console", lambda m: m.type == "error" and problems.append(f"console: {m.text}"))
                page.goto(base + "/")
                page.wait_for_function("document.querySelectorAll('#questions .q').length >= 2")

                card = page.locator("#questions .q", has_text="leaning fence")
                card.locator("button.mic").click()
                page.wait_for_selector("#armstrip:not([hidden])")
                strip = page.inner_text("#armstrip")
                assert "Hold F9 and answer" in strip and "leaning fence" in strip and " s" in strip, strip
                page.wait_for_function("document.querySelector('#questions .q.armed') !== null")
                assert card.locator("button.mic").inner_text() == "Cancel 🎤"
                armed = json.loads(home.answer_arm.read_text(encoding="utf-8"))
                assert armed["tick"] == "md" and armed["question"].startswith("How much does the leaning fence")
                page.screenshot(path=str(shots / "panel-armed-640.png"))

                page.click("#tabs button[data-tab=dumps]")                 # the strip stays up on every tab
                assert page.is_visible("#armstrip")
                page.keyboard.press("Escape")
                page.wait_for_selector("#armstrip", state="hidden")
                wait_for(lambda: not home.answer_arm.exists(), 5, "Esc to remove the arm")

                page.wait_for_function("document.querySelectorAll('#dumplist .dl').length === 2")
                page.click("#dumplist .dl.ready")
                page.wait_for_selector("#dumpdetail h3")
                q = page.locator("#dumpdetail .q", has_text="lumber yard quote")
                q.locator("button.mic").click()
                page.wait_for_selector("#armstrip:not([hidden])")
                armed = json.loads(home.answer_arm.read_text(encoding="utf-8"))
                assert armed["dump"] == READY and armed["question"].startswith("What did the lumber yard quote")
                page.wait_for_function("document.querySelector('#dumpdetail .q.armed') !== null")
                q.locator("button.mic").click()                           # the same button cancels it
                page.wait_for_selector("#armstrip", state="hidden")
                wait_for(lambda: not home.answer_arm.exists(), 5, "Cancel to remove the arm")
                assert inbox_texts(home) == []                            # arming typed nothing
            finally:
                browser.close()
    finally:
        ptt.stop.set()
        proc.kill()
        proc.wait(timeout=10)
    assert problems == [], "\n".join(problems) + "\n--- server log ---\n" + log.read_text(encoding="utf-8")[-3000:]

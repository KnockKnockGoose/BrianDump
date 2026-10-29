"""QA findings of the v0.1 review (an internal report, not part of the public repo), one strict xfail test each.

    py -m pytest tests/test_qa_findings.py -q

Each test asserts the behaviour the finding asks for, so it fails today and is marked xfail(strict=True): the fix makes it pass,
pytest reports XPASS as a failure, and whoever fixed it removes the marker. Everything runs in conftest's sandboxed `home`
(a throwaway BrianDump home, BRIANDUMP_SANDBOX=1, FakeWin instead of a console). No model calls, no keys, no microphone.

Two tests drive the page in Chromium and skip when Playwright's browser is not installed (as in CI). The personal-data test
reads the owner's private word list from the gitignored qa/scrub_scan.py and skips when that file is not in the checkout, so
this file never carries the list itself.
"""
import ast
import importlib.util
import json
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from briandump import cli, dumps, server, sessions, voice

REPO = Path(__file__).resolve().parents[1]
MAIN_SID, DOCS_SID = "11111111-2222-4333-8444-555555555555", "66666666-7777-4888-8999-000000000000"


# ---------- helpers ----------
@pytest.fixture(autouse=True)
def fresh_server(monkeypatch):
    """The server module's per-process caches start empty in every test."""
    for name, value in (("TRACKERS", {}), ("KNOWN_QS", {}), ("_DETAILS", {}), ("_LAST", {}), ("_LOGGED", {}),
                        ("_DUMPS", {"t": 0.0, "dir": None, "rows": []}), ("RELOAD", [0]), ("VOICE", None), ("SESSION", None)):
        monkeypatch.setattr(server, name, value)


@pytest.fixture
def api(home):
    """The panel API in this process, on a free loopback port: -> the port."""
    httpd = server.Server(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def request(port, method, path, body=None, host=None, origin=None):
    """A hand-made HTTP request (any Host / Origin header): -> (status, parsed body)."""
    data = json.dumps(body).encode("utf-8") if body is not None else b""
    head = [f"{method} {path} HTTP/1.1", f"Host: {host or f'127.0.0.1:{port}'}", "Connection: close"]
    if origin:
        head.append(f"Origin: {origin}")
    if body is not None:
        head += ["Content-Type: application/json", f"Content-Length: {len(data)}"]
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + data)
        raw = b""
        while chunk := s.recv(65536):
            raw += chunk
    status_line, _, rest = raw.partition(b"\r\n")
    payload = rest.partition(b"\r\n\r\n")[2]
    try:
        parsed = json.loads(payload or b"{}")
    except ValueError:
        parsed = payload
    return int(status_line.split()[1]), parsed


def inbox_lines(home):
    return [p.read_text(encoding="utf-8") for p in sorted(home.inbox.glob(f"*-panel-{home.host}.txt"))]


def ts(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"


def user(text, t):
    return {"type": "user", "message": {"role": "user", "content": text}, "timestamp": ts(t)}


def said(text, t):
    return {"type": "assistant", "timestamp": ts(t), "message": {"role": "assistant", "model": "claude-test-1",
                                                                  "content": [{"type": "text", "text": text}]}}


def turn_end(t):
    return {"type": "system", "subtype": "turn_duration", "durationMs": 900, "timestamp": ts(t)}


def ask(tid, t, question, options):
    inp = {"questions": [{"question": question, "header": "Q", "multiSelect": False,
                          "options": [{"label": o, "description": o} for o in options]}]}
    block = {"type": "tool_use", "id": tid, "name": "AskUserQuestion", "input": inp}
    return {"type": "assistant", "timestamp": ts(t), "message": {"role": "assistant", "model": "claude-test-1", "content": [block]}}


def result(tid, t, text):
    return {"type": "user", "timestamp": ts(t), "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                                                                           "content": text}]}}


def session(home, pid, sid, name, lines, project, mtime):
    """A live session: its registry record and its transcript (with the given mtime)."""
    (home.sessions / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": sid, "cwd": str(home.state_dir), "name": name,
                                                           "entrypoint": "cli", "status": "idle", "startedAt": 1_792_000_000_000}),
                                               encoding="utf-8")
    tr = home.projects / project / f"{sid}.jsonl"
    tr.parent.mkdir(parents=True, exist_ok=True)
    tr.write_text("".join(json.dumps(x, separators=(",", ":")) + "\n" for x in lines), encoding="utf-8")
    os.utime(tr, (mtime, mtime))
    return tr


def append(tr, *lines):
    with open(tr, "a", encoding="utf-8") as f:
        for x in lines:
            f.write(json.dumps(x, separators=(",", ":")) + "\n")


def users(tr):
    out = []
    for ln in tr.read_text(encoding="utf-8").splitlines():
        e = json.loads(ln)
        if e.get("type") == "user" and isinstance(e["message"].get("content"), str):
            out.append(e["message"]["content"])
    return out


def router(home, now):
    """The voice router of the sandbox (FakeWin types into the fixture transcripts), past its start-up grace."""
    r = voice.Router(lambda: sessions.rows(), host=home.host, clock=lambda: now[0], sleep=lambda s: None)
    r.t0 = now[0] - voice.HANDOFF_GRACE_S - 1
    return r


def write_meta(home, did, **meta):
    d = home.dumps_dir / did
    d.mkdir(parents=True, exist_ok=True)
    m = {"id": did, "createdAt": "2026-10-18T09:30:00+00:00", "updatedAt": "2026-10-18T09:31:00+00:00", "durationSec": 60.0,
         "status": "ready", "error": None, "attempts": 0, "timings": {}, "words": 10, "source": "ptt", "label": None, "host": home.host,
         "sent": {}, "agenda_added": [], "questions_added": []}
    m.update(meta)
    (d / "meta.json").write_text(json.dumps(m), encoding="utf-8")
    return d


def chromium():
    """(playwright, browser), or skip: the page tests need Playwright's Chromium (not installed in CI)."""
    sync_api = pytest.importorskip("playwright.sync_api")
    pw = None
    try:
        pw = sync_api.sync_playwright().start()
        return pw, pw.chromium.launch()
    except Exception as e:  # noqa: BLE001 - no driver, no browser binary, no display libraries: not this test's subject
        if pw is not None:
            pw.stop()
        pytest.skip(f"Playwright's Chromium is not available ({type(e).__name__})")


# ---------- High ----------
def test_qa01_rebinding_host_is_refused(home, api):
    rebind = f"{home.host}.attacker.example:{api}"
    get_code, _ = request(api, "GET", "/api/state", host=rebind)
    post_code, _ = request(api, "POST", "/api/answer", {"text": "typed by a web page"}, host=rebind, origin=f"http://{rebind}")
    assert (get_code, post_code) == (403, 403) and not inbox_lines(home)


def test_qa02_answer_goes_to_the_session_that_asked(home, api):
    now = time.time()
    main = session(home, 4242, MAIN_SID, "Main", [user("plan the shed", now - 300), said("Done.", now - 299), turn_end(now - 298)],
                   "C--work-shed", now - 200)
    question = said("Should I archive the old docs folder? Say yes or no.", now - 59)
    docs = session(home, 4243, DOCS_SID, "Docs", [user("tidy the docs", now - 60), question, turn_end(now - 58)], "C--work-docs", now - 10)
    qid = next(q["id"] for q in server.full_state()["questions"] if "archive" in q["text"])
    assert request(api, "POST", "/api/answer", {"id": qid, "text": "yes, archive it"})[0] == 200
    clock = [time.time() + 5]
    router(home, clock).tick(clock[0])
    assert any("yes, archive it" in t for t in users(docs)) and not any("yes, archive it" in t for t in users(main))


@pytest.mark.parametrize("argv", [["config", "--set", "ptt.key", "f7"], ["config", "--init"]], ids=["set", "init"])
def test_qa03_config_writes_keep_a_config_with_a_syntax_error(home, argv):
    broken = '{\n  "port": 8771,\n  "private_filter": ["garden"],\n  "dump": {"user_name": "Sam"},\n}\n'
    home.config_file.write_text(broken, encoding="utf-8")
    cli.main(argv)
    after = home.config_file.read_text(encoding="utf-8")
    try:
        kept = json.loads(after).get("port") == 8771 and json.loads(after).get("private_filter") == ["garden"]
    except ValueError:
        kept = False
    assert after == broken or kept, after


def _tracked_files():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=str(REPO), capture_output=True, text=True, timeout=30, check=True).stdout
        files = [ln for ln in out.splitlines() if ln.strip()]
        if files:
            return files
    except (OSError, subprocess.SubprocessError):
        pass
    skip = {".git", "qa", "__pycache__", ".ruff_cache", ".pytest_cache"}
    return [p.relative_to(REPO).as_posix() for p in REPO.rglob("*") if p.is_file() and not skip & set(p.relative_to(REPO).parts)]


def _folded_strings(source):
    """String concatenations ("a" + "b") folded into one string: a split word is still the word."""
    out = []

    def fold(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = fold(node.left), fold(node.right)
            if left is not None and right is not None:
                return left + right
        return None
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            s = fold(node)
            if s is not None:
                out.append(s)
    return out


def _allowed(rel, line, kind):
    """The release rule: the owner's name only in LICENSE, the README byline, the author fields and a report's byline."""
    if kind != "owner-name":
        return False
    if rel == "LICENSE" or (rel == "pyproject.toml" and line.lstrip().startswith("authors")):
        return True
    if rel.endswith(("plugin.json", "marketplace.json")) and line.strip().startswith('"name"'):
        return True
    if rel == "README.md" and "GitHub [" in line:
        return True
    return rel.startswith("docs/") and line.startswith("Prepared for ")


def test_qa04_no_personal_data_in_tracked_files():
    scan = REPO / "qa" / "scrub_scan.py"
    if not scan.is_file():
        pytest.skip("the private word list (qa/scrub_scan.py, gitignored) is not in this checkout")
    spec = importlib.util.spec_from_file_location("scrub_scan_private", scan)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    hits = []
    for rel in _tracked_files():
        p = REPO / rel
        if p.suffix.lower() in {".ico", ".png", ".gif", ".jpg", ".wav"} or not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        if p.suffix in (".py", ".pyw"):
            try:
                lines += _folded_strings(text)
            except SyntaxError:
                pass
        for line in lines:
            for kind, rx in mod.PATTERNS:
                if rx.search(line) and not _allowed(rel, line, kind):
                    hits.append((rel, kind))
    assert not hits, sorted(set(hits))


# ---------- Medium ----------
def test_qa05_panel_answer_to_a_closed_dialog_is_not_typed_later(home, api):
    now = time.time()
    dialog = ask("toolu_qa05", now - 29, "Which door?", ("Single", "Double"))
    main = session(home, 4242, MAIN_SID, "Main", [user("pick a door", now - 30), dialog], "C--work-shed", now - 20)
    qid = next(q["id"] for q in server.full_state()["questions"] if q.get("source") == "asked")
    assert request(api, "POST", "/api/answer", {"id": qid, "text": "Double"})[0] in (200, 400, 409)
    clock = [time.time() + 5]
    r = router(home, clock)
    r.tick(clock[0])                                                       # the dialog is open: the line waits
    t = time.time()
    append(main, result("toolu_qa05", t, "User answered in the terminal: Single"), said("Single it is.", t + 0.5), turn_end(t + 0.6))
    clock[0] += voice.SETTLE_S + 1
    r.tick(clock[0])
    assert not [x for x in users(main) if x.endswith("] Double")]


def test_qa06_masked_option_is_not_typed_as_the_placeholder(home, api):
    home.config_file.write_text(json.dumps({"private_filter": ["(?i)zebra"]}), encoding="utf-8")
    home.__init__(env=home._env)
    asked = time.strftime("%Y-%m-%dT%H:%M:%S")
    home.manual_questions.write_text(json.dumps([{"id": "q3", "text": "Ship the shed plan now?", "kind": "ask",
                                                  "options": ["Ship it", "Hold for the zebra review"], "asked": asked, "done": False}]),
                                     encoding="utf-8")
    q3 = next(q for q in server.full_state()["questions"] if q["id"] == "q3")
    assert q3["options"] == ["Ship it", "(private)"]
    request(api, "POST", "/api/answer", {"id": "q3", "text": q3["options"][1]})    # what the button sends
    assert not [x for x in inbox_lines(home) if x.rstrip().endswith("(private)")]


@pytest.mark.parametrize("cfg", [{"private_filter": ["(draft"]}, {"port": "eighty"}], ids=["regex", "port"])
def test_qa07_a_bad_config_value_does_not_crash_at_import(home, cfg):
    from briandump.config import Config
    home.config_file.write_text(json.dumps(cfg), encoding="utf-8")
    c = Config(env=dict(os.environ))
    assert c.port and c.private_re.search("") is None


def test_qa08_a_ticked_agenda_row_stays_where_it_was(home, api):
    home.agenda_file.write_text("# Agenda\n\n" + "".join(f"- [ ] Shed task {i}\n" for i in range(1, 7)), encoding="utf-8")
    pw, browser = chromium()
    try:
        page = browser.new_page(viewport={"width": 853, "height": 1000})
        page.goto(f"http://127.0.0.1:{api}/")
        page.wait_for_selector("#agenda .ag", timeout=15000)
        page.locator("#agenda .ag").nth(1).click()
        page.wait_for_timeout(1500)
        rows = page.eval_on_selector_all("#agenda .ag .h", "els => els.map(e => e.textContent)")
    finally:
        browser.close()
        pw.stop()
    assert rows[1] == "Shed task 2", rows


# ---------- Low ----------
def test_qa09_a_long_answer_is_refused_or_arrives_whole(home, api):
    home.manual_questions.write_text(json.dumps([{"id": "q2", "text": "What colour should the door be?", "kind": "think", "options": [],
                                                  "asked": time.strftime("%Y-%m-%dT%H:%M:%S"), "done": False}]), encoding="utf-8")
    text = "L" * 3985 + " END-MARKER"
    code, _ = request(api, "POST", "/api/answer", {"id": "q2", "text": text})
    assert code >= 400 or voice.compose("panel", inbox_lines(home)[-1]).endswith("END-MARKER")


def test_qa10_the_server_leaves_a_dump_the_live_recorder_is_transcribing(home):
    np = pytest.importorskip("numpy")
    from briandump import ptt
    _store, did = ptt.save_dump(np.zeros(16000, dtype="float32"), time.time() - 5, dumps, log=lambda m: None)
    meta = dumps.read_meta(did)
    assert meta["status"] == "transcribing"                               # this process plays the recorder and is alive
    assert dumps._job(meta, time.time() + 16 * 60) is None


def test_qa11_a_dump_with_a_broken_meta_is_still_listed(home):
    d = home.dumps_dir / "20261018-095000-a006"
    d.mkdir(parents=True)
    (d / "meta.json").write_text('{"id": "20261018-095000-a006", "status": ', encoding="utf-8")
    (d / "transcript.txt").write_text("measure the shed\n", encoding="utf-8")
    assert "20261018-095000-a006" in [r["id"] for r in dumps.listing()]


def test_qa12_zero_width_joiners_survive():
    text = "\U0001F469‍\U0001F4BB fixed it, می‌خواهم"
    out = voice.compose("panel", text)
    assert "‍" in out and "‌" in out


@pytest.mark.xfail(reason="QA-13 sessions.Tail reads a transcript's whole unread part in one call: about twice the file size in memory",
                   strict=True)
def test_qa13_tail_reads_a_big_transcript_in_bounded_memory(home):
    import tracemalloc
    tr = home.projects / "C--work-big" / "77777777-0000-4000-8000-000000000007.jsonl"
    tr.parent.mkdir(parents=True)
    line = json.dumps({"type": "user", "timestamp": ts(time.time()), "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_big", "content": "x" * 150_000}]}}, separators=(",", ":")) + "\n"
    with open(tr, "w", encoding="utf-8", newline="\n") as f:
        for _ in range(120):                                               # about 18 MB
            f.write(line)
    tail = sessions.Tail(tr)
    tracemalloc.start()
    try:
        tail.update()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert tail.offset == tr.stat().st_size and peak < 8 * 2**20, f"peak {peak / 2**20:.0f} MB"


def test_qa14_the_open_main_reply_stays_readable(home, api):
    pw, browser = chromium()
    try:
        page = browser.new_page(viewport={"width": 853, "height": 1000})
        page.goto(f"http://127.0.0.1:{api}/#voice")
        page.wait_for_selector("#voicestrip .openmain", timeout=15000)
        page.click("#voicestrip .openmain")
        page.wait_for_timeout(1500)
        msg = page.inner_text("#vmsg") if page.locator("#vmsg").count() else ""
    finally:
        browser.close()
        pw.stop()
    assert msg.strip()


def test_qa15_dump_counts_leave_out_other_pcs(home):
    write_meta(home, "20261018-092000-a003", status="transcribing", host="otherpc", updatedAt=time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                                                                                         time.gmtime()))
    counts = server.dump_counts()
    assert counts["transcribing"] == 0 and counts["processing"] == 0, counts


def test_qa16_doctor_does_not_fail_a_declined_talk_key(home, monkeypatch):
    voice_pkgs = {"faster_whisper", "sounddevice", "pynput", "numpy"}
    monkeypatch.setattr(cli, "_on_windows", lambda: True)
    monkeypatch.setattr(cli, "_task_info", lambda name: None)
    monkeypatch.setattr(cli, "_import_ok", lambda m: (False, "ModuleNotFoundError") if m in voice_pkgs else (True, ""))
    rows = {c.name: c for c in cli.run_checks([cli.check_packages, cli.check_task])}
    assert rows["Packages"].ok is not False and rows["Talk-key task"].ok is not False, rows


def _doc(rel):
    return (REPO / rel).read_text(encoding="utf-8")


def _quotes_the_question(home, port):
    home.questions_file.write_text("# Questions\n\n- [ ] Which wall gets the shelves?\n", encoding="utf-8")
    qid = next(q["id"] for q in server.full_state()["questions"] if "shelves" in q["text"])
    request(port, "POST", "/api/answer", {"id": qid, "text": "north"})
    return any("Which wall gets the shelves?" in x for x in inbox_lines(home))


def test_qa18_internal_qa_reports_stay_out_of_the_repo():
    """QA-18 first asked for docs/qa/ to be tracked; since the public release the QA reports are internal and ignored."""
    try:
        r = subprocess.run(["git", "check-ignore", "-q", "docs/qa/a-later-qa-report.md"], cwd=str(REPO), capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git is not available")
    if r.returncode not in (0, 1):
        pytest.skip("not a git checkout")
    assert r.returncode == 0                                   # 0 = ignored


@pytest.mark.parametrize("claim", ["install-default", "arrives-twice", "ask-chime", "security-claim"])
def test_qa17_docs_match_the_code(home, api, claim):
    if claim == "install-default":                     # install.ps1 asks with $PanelMode as the default (yes in panel mode)
        line = next(ln for ln in _doc("docs/install.md").splitlines() if "Start the panel server every time you" in ln)
        assert "(no)" not in line, line
    elif claim == "arrives-twice":                     # ptt.single_instance(): a second recorder exits with code 4
        assert "arrives twice" not in _doc("README.md") + _doc("docs/install.md")
    elif claim == "ask-chime":                         # the page chimes only when Claude needs you, not for a new question
        assert "with a chime" not in _doc("plugin/commands/ask.md")
    else:                                              # answer lines quote the question text BrianDump read from a file
        absolute = re.search(r"Nothing BrianDump reads.{0,80}is ever typed", _doc("SECURITY.md"), re.S)
        assert not (absolute and _quotes_the_question(home, api))

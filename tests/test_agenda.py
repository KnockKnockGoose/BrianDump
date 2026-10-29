"""agenda.py: markdown checklists (parse, add with dedupe, tick) that never disturb the rest of the file."""
import threading
import time

import pytest

from briandump import agenda

NOTE = "(from brain dump 2026-10-18 10:42)"


def write(p, text):
    p.write_bytes(text.encode("utf-8"))
    return p


def test_parse_items_done_and_lines():
    text = "# Agenda\n\n- [ ] one\n* [x] two\n+ [X] three\n  - [ ] nested\n- plain bullet\n- [ ]\n-[ ] no space\ntext\n"
    items = agenda.parse(text)
    assert [(i["text"], i["done"], i["line"]) for i in items] == [
        ("one", False, 2), ("two", True, 3), ("three", True, 4), ("nested", False, 5)]


def test_parse_heading_scope_any_level_until_a_sibling():
    text = ("# Notes\n- [ ] top\n## Work\n- [ ] w1\n### Sub\n- [ ] w2\n## Home\n- [ ] h1\n"
            "## work:\n- [ ] w3\n")
    assert [i["text"] for i in agenda.parse(text, "Work")] == ["w1", "w2", "w3"]
    assert [i["text"] for i in agenda.parse(text, "home")] == ["h1"]
    assert agenda.parse(text, "Missing") == []
    assert len(agenda.parse(text)) == 5


def test_parse_ignores_fenced_code_and_strips_the_note():
    text = f"- [ ] real {NOTE}\n```\n- [ ] in code\n## not a heading\n```\n~~~~\n- [x] also code\n~~~~\n"
    items = agenda.parse(text)
    assert [i["text"] for i in items] == [f"real {NOTE}"]
    assert items[0]["base"] == "real"
    assert agenda.parse(text, "not a heading") == []


def test_norm_ignores_case_punctuation_apostrophes_and_the_note():
    assert agenda.norm("  Don't   forget: the MILK!! ") == "dont forget the milk"
    assert agenda.norm("Buy nails " + NOTE) == agenda.norm("buy nails.")
    assert agenda.norm("Straße") == agenda.norm("STRASSE")
    assert agenda.norm("café crème") == "café crème"
    assert agenda.norm("(from brain dump 10-18)") == ""


def test_same_rule():
    long_a, long_b = agenda.norm("call the lumber yard tomorrow"), agenda.norm("call the lumber yard tomorrow morning")
    assert agenda.same(long_a, long_b)
    assert not agenda.same("buy nails", "buy nails and screws")          # short: equality only
    assert not agenda.same(agenda.norm("measure the shed spaces now"), agenda.norm("re measure the shed space now please"))


def test_add_creates_the_file_with_a_header(tmp_path):
    p = tmp_path / "sub" / "agenda.md"
    assert agenda.add(p, ["Buy screws", "  Call   the\nyard "]) == ["Buy screws", "Call the yard"]
    assert p.read_text(encoding="utf-8") == agenda.HEADERS["agenda"] + "- [ ] Buy screws\n- [ ] Call the yard\n"
    q = tmp_path / "questions.md"
    agenda.add(q, ["What next?"])
    assert q.read_text(encoding="utf-8").startswith("# Questions\n")
    assert agenda.add(tmp_path / "none.md", []) == [] and not (tmp_path / "none.md").exists()


def test_add_dedupes_against_open_items_and_within_the_batch(tmp_path):
    p = write(tmp_path / "agenda.md", "# A\n\n- [ ] Buy screws\n- [x] Water the plants\n"
                                      "- [ ] Call the lumber yard tomorrow morning for a quote\n")
    added = agenda.add(p, ["buy SCREWS!", "Water the plants", "call the lumber yard tomorrow morning",
                           "Buy nails", "buy nails.", "Paint the shed"])
    assert added == ["Water the plants", "Buy nails", "Paint the shed"]      # a done item may come back
    assert agenda.open_items(p) == ["Buy screws", "Call the lumber yard tomorrow morning for a quote",
                                    "Water the plants", "Buy nails", "Paint the shed"]


def test_short_item_with_the_note_still_dedupes(tmp_path):
    p = tmp_path / "agenda.md"
    assert agenda.add(p, ["Buy nails"], suffix=NOTE) == ["Buy nails"]
    assert p.read_text(encoding="utf-8").endswith(f"- [ ] Buy nails {NOTE}\n")
    assert agenda.add(p, ["buy nails"]) == []
    assert agenda.add(p, ["Buy nails"], suffix="(from brain dump 2026-10-19 09:00)") == []
    assert agenda.add(p, ["Buy glue"], suffix="[via panel]") == ["Buy glue"]
    assert agenda.add(p, ["buy glue"], suffix="[via panel]") == []              # a custom suffix is ignored too


def test_add_under_a_heading(tmp_path):
    p = write(tmp_path / "notes.md", "# Notes\n\n## Work\n- [ ] w1\n  more about w1\n\n## Home\n- [ ] h1\n")
    assert agenda.add(p, ["w2"], heading="work") == ["w2"]
    assert agenda.add(p, ["h1"], heading="Work") == ["h1"]                    # h1 is open, but under another heading
    assert p.read_text(encoding="utf-8") == ("# Notes\n\n## Work\n- [ ] w1\n  more about w1\n- [ ] w2\n- [ ] h1\n\n"
                                            "## Home\n- [ ] h1\n")
    assert agenda.add(p, ["g1"], heading="Garden") == ["g1"]
    assert p.read_text(encoding="utf-8").endswith("## Home\n- [ ] h1\n\n## Garden\n\n- [ ] g1\n")
    empty = write(tmp_path / "e.md", "## Today\n\n## Later\n- [ ] x\n")
    agenda.add(empty, ["t1"], heading="Today")
    assert empty.read_text(encoding="utf-8") == "## Today\n\n- [ ] t1\n\n## Later\n- [ ] x\n"
    new = tmp_path / "new-agenda.md"
    agenda.add(new, ["n1"], heading="Inbox")
    assert new.read_text(encoding="utf-8") == agenda.HEADERS["agenda"] + "## Inbox\n\n- [ ] n1\n"


def test_add_keeps_every_other_byte(tmp_path):
    body = "﻿# Agenda\r\n\r\n- [ ] one\r\n\r\nNotes: caf\xe9 ☕\r\n```\r\n- [ ] code\r\n```"
    p = tmp_path / "agenda.md"
    p.write_bytes(body.encode("utf-8") + b"\r\nlegacy \x96 byte\r\n")
    agenda.add(p, ["two"])
    data = p.read_bytes()
    assert data == ("﻿# Agenda\r\n\r\n- [ ] one\r\n- [ ] two\r\n\r\nNotes: caf\xe9 ☕\r\n```\r\n- [ ] code\r\n```"
                    ).encode("utf-8") + b"\r\nlegacy \x96 byte\r\n"
    tail = write(tmp_path / "t.md", "# T\n\nintro text")
    agenda.add(tail, ["x"])
    assert tail.read_text(encoding="utf-8") == "# T\n\nintro text\n\n- [ ] x\n"
    blank = write(tmp_path / "b.md", "")
    agenda.add(blank, ["x"])
    assert blank.read_text(encoding="utf-8") == "- [ ] x\n"


def test_add_strips_a_pasted_checkbox(tmp_path):
    p = tmp_path / "agenda.md"
    assert agenda.add(p, ["- [ ] Fix the gate", "[x] Oil the hinge"]) == ["Fix the gate", "Oil the hinge"]


def test_set_done_round_trip_changes_one_character(tmp_path):
    original = f"# A\n\n- [ ] Measure the space {NOTE}\n- [ ] Buy nails\n  * [ ] Nested one\n"
    p = write(tmp_path / "agenda.md", original)
    assert agenda.set_done(p, "measure the space") is True
    after = p.read_text(encoding="utf-8")
    assert after == original.replace("- [ ] Measure", "- [x] Measure")
    assert agenda.set_done(p, f"Measure the space {NOTE}") is True              # already done: no change
    assert p.read_text(encoding="utf-8") == after
    assert agenda.set_done(p, "nested ONE.", True) is True
    assert "  * [x] Nested one" in p.read_text(encoding="utf-8")
    assert agenda.set_done(p, "Measure the space", done=False) is True
    assert agenda.set_done(p, "Nested one", False) is True
    assert p.read_text(encoding="utf-8") == original
    assert agenda.set_done(p, "nothing like this") is False
    assert agenda.set_done(p, "") is False
    assert agenda.set_done(tmp_path / "missing.md", "x") is False


def test_set_done_prefers_the_copy_in_the_other_state(tmp_path):
    p = write(tmp_path / "agenda.md", "- [x] Water the plants\n- [ ] Water the plants\n")
    assert agenda.set_done(p, "Water the plants") is True
    assert p.read_text(encoding="utf-8") == "- [x] Water the plants\n- [x] Water the plants\n"


def test_set_done_with_heading_and_crlf_and_bom(tmp_path):
    p = tmp_path / "a.md"
    p.write_bytes("﻿- [ ] first\r\n## Work\r\n- [ ] first\r\n".encode("utf-8"))
    assert agenda.set_done(p, "first", heading="Work") is True
    assert p.read_bytes() == "﻿- [ ] first\r\n## Work\r\n- [x] first\r\n".encode("utf-8")
    assert agenda.set_done(p, "first") is True
    assert p.read_bytes() == "﻿- [x] first\r\n## Work\r\n- [x] first\r\n".encode("utf-8")


def test_unicode_items(tmp_path):
    p = tmp_path / "agenda.md"
    texts = ["Réparer la clôture", "買い物リストを作る", "Grüße an die Nachbarn \U0001F44B"]
    assert agenda.add(p, texts) == texts
    assert [i["text"] for i in agenda.read(p)] == texts
    assert agenda.add(p, ["RÉPARER LA CLÔTURE"]) == []
    assert agenda.set_done(p, "買い物リストを作る") is True
    assert [i["done"] for i in agenda.read(p)] == [False, True, False]


def test_read_missing_file_is_empty(tmp_path):
    assert agenda.read(tmp_path / "nope.md") == []
    assert agenda.open_items(tmp_path / "nope.md") == []


def test_redoes_the_edit_when_the_file_changed_underneath(tmp_path, monkeypatch):
    p = write(tmp_path / "agenda.md", "- [ ] one\n")
    real_raw, calls = agenda._raw, {"n": 0}

    def raw(path):
        calls["n"] += 1
        if calls["n"] == 2:                                   # between our read and our write, an editor saved the file
            write(p, "- [ ] one\n- [ ] typed by hand\n")
        return real_raw(path)
    monkeypatch.setattr(agenda, "_raw", raw)
    assert agenda.add(p, ["two"]) == ["two"]
    assert p.read_text(encoding="utf-8") == "- [ ] one\n- [ ] typed by hand\n- [ ] two\n"


def test_writes_survive_concurrent_readers(tmp_path):
    p = tmp_path / "agenda.md"
    agenda.add(p, ["seed"])
    stop, errors = threading.Event(), []

    def outsider():                                           # an editor / sync client: holds the file open now and then
        while not stop.is_set():
            try:
                with open(p, encoding="utf-8") as f:
                    f.read()
            except OSError:
                pass                                          # its own problem (Windows refuses opens mid-swap)
            time.sleep(0.001)

    def reader():                                             # BrianDump's own reads must never fail or come back empty
        while not stop.is_set():
            try:
                if not agenda.read(p):
                    errors.append("empty read")
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
            time.sleep(0.001)
    threads = [threading.Thread(target=f, daemon=True) for f in (outsider, outsider, reader)]
    for t in threads:
        t.start()
    try:
        for n in range(60):
            agenda.add(p, [f"item number {n}"])
            if n % 3 == 0:
                agenda.set_done(p, f"item number {n}")
    finally:
        stop.set()
        for t in threads:
            t.join(5)
    assert errors == []
    items = agenda.read(p)
    assert len(items) == 61 and sum(i["done"] for i in items) == 20
    assert not [x.name for x in tmp_path.iterdir() if x.name.endswith(".tmp")]


def test_replace_retry_gives_up_and_cleans_up(tmp_path, monkeypatch):
    p = write(tmp_path / "agenda.md", "- [ ] keep me\n")

    def refuse(src, dst):
        raise PermissionError(5, "Access is denied")
    monkeypatch.setattr(agenda.os, "replace", refuse)
    monkeypatch.setattr(agenda, "REPLACE_PAUSE_S", 0)
    with pytest.raises(PermissionError):
        agenda.write_text_atomic(p, "new", errors="strict")
    assert p.read_text(encoding="utf-8") == "- [ ] keep me\n"
    assert [x.name for x in tmp_path.iterdir()] == ["agenda.md"]

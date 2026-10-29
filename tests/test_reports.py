"""Tests for briandump/reports.py and the Reports tab's API: what is listed (brain-dump reports always; an index, roots and a
byline search when configured), the order, read / unread, and that nothing outside the configured folders is ever listed or
served (traversal, symlinks, junctions, ids that are not in the listing). The renderer is covered by tests/test_mdhtml.py.

    py -m pytest tests/test_reports.py -q

Everything runs against conftest's `home` (a throwaway BrianDump home, sandbox on). Report folders are made per test under
tmp_path; `configure` writes the reports section into config.json and re-reads it, the way a restart would.
"""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from briandump import reports, server

WIN = sys.platform == "win32"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
DAY = 86400


# ---------- helpers ----------
@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """The discovery cache and the file-info cache start empty in every test."""
    monkeypatch.setattr(reports, "_found", {"key": None, "t": 0.0, "paths": [], "warnings": []})
    monkeypatch.setattr(reports, "_info", {})


@pytest.fixture
def api(home):
    httpd = server.Server(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def call(method, url, body=None, headers=None):
    hdrs, data = dict(headers or {}), None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with OPENER.open(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def configure(home, **section):
    home.config_file.write_text(json.dumps({"reports": section}), encoding="utf-8")
    home.__init__(env=home._env)
    return home


def write(path, text, age_days=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if age_days is not None:
        t = time.time() - age_days * DAY
        os.utime(path, (t, t))
    return path


def dump_report(home, did="20261018-093000-a1b2", slug="garden-shed"):
    return write(home.reports_dir / f"{did}-{slug}.md", "# Brain dump · 2026-10-18 09:30 (4 min)\n\n## Summary\n\nShed talk.\n")


def items(listing=None):
    return (listing or reports.listing())["items"]


def titles(listing=None):
    return [r["title"] for r in items(listing)]


BYLINE = "Prepared for Sam · Claude · 2026-10-19"


# ---------- what is listed ----------
def test_a_new_install_lists_its_brain_dump_reports_only(home, tmp_path):
    dump_report(home)
    write(tmp_path / "projects" / "shed" / "plan.md", f"# A plan\n\n{BYLINE}\n")       # not configured: not looked at
    out = reports.listing()
    assert out["configured"] is False and out["warnings"] == [] and out["unread"] == 1
    (row,) = out["items"]
    assert row["title"] == "Brain dump · 2026-10-18 09:30 (4 min)" and row["date"] == "2026-10-18"
    assert row["by"] == "BrianDump" and row["source"] == "dump" and row["where"] == ""
    assert row["path"] == "reports/20261018-093000-a1b2-garden-shed.md"
    assert row["unread"] is True and row["viewable"] is True and re.fullmatch(r"[0-9a-f]{12}", row["id"])
    assert "_p" not in row and not any(str(tmp_path) in str(v) for v in row.values())     # no absolute path leaves


def test_index_entries_resolve_against_the_roots_in_order(home, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write(b / "shed" / "docs" / "plan.md", "# The shed plan\n\nbody\n")
    write(a / "shed" / "notes.md", "# Notes heading\n")
    write(b / "shed" / "notes.md", "# The other notes\n")
    write(a / "loose.md", "no heading here\n")
    index = write(tmp_path / "index.json", "\ufeff" + json.dumps([
        {"path": "shed/docs/plan.md", "title": "Plan (from the index)", "date": "2026-10-19", "by": "Main"},
        {"path": "shed/notes.md"},
        "loose.md",
        {"path": 42}, {"title": "no path"}, None, {"path": ""},
    ]))
    configure(home, index=str(index), roots=[str(a), str(b)])
    rows = {r["path"]: r for r in items()}
    assert set(rows) == {"shed/docs/plan.md", "shed/notes.md", "loose.md"}
    plan = rows["shed/docs/plan.md"]
    assert (plan["title"], plan["date"], plan["by"], plan["where"], plan["source"]) == \
        ("Plan (from the index)", "2026-10-19", "Main", "shed", "index")
    assert rows["shed/notes.md"]["title"] == "Notes heading"                  # the first root that has it wins
    assert rows["loose.md"]["title"] == "loose" and rows["loose.md"]["where"] == "a"


def test_index_can_be_a_list_of_files_and_tilde_and_variables_expand(home, tmp_path, monkeypatch):
    root = tmp_path / "root"
    write(root / "one.md", "# One\n")
    write(root / "two.md", "# Two\n")
    write(tmp_path / "i1.json", json.dumps(["one.md"]))
    write(tmp_path / "i2.json", json.dumps(["two.md"]))
    monkeypatch.setenv("BRIANDUMP_TEST_IDX", str(tmp_path))
    configure(home, index=["%BRIANDUMP_TEST_IDX%/i1.json" if WIN else "$BRIANDUMP_TEST_IDX/i1.json", str(tmp_path / "i2.json")],
              roots=[str(root)])
    assert sorted(titles()) == ["One", "Two"]


def test_newest_first_by_date_then_by_modified_time(home, tmp_path):
    root = tmp_path / "root"
    write(root / "old.md", "# Old\n\n2026-10-17\n")
    write(root / "same-day-earlier.md", "# Earlier\n\n2026-10-19\n", age_days=0.5)
    write(root / "same-day-later.md", "# Later\n\n2026-10-19\n", age_days=0.1)
    write(root / "2026-10-18-named.md", "# Named\n\nwritten 2026-10-01\n")      # the file name's date beats the text's
    configure(home, roots=[str(root)], discover=r"^\d{4}-\d\d-\d\d|written")
    assert titles() == ["Later", "Earlier", "Named", "Old"]


def test_discover_reads_only_the_first_lines_and_skips_noise(home, tmp_path):
    root = tmp_path / "root"
    write(root / "shed" / "a.md", f"# Found\n\n{BYLINE}\n")
    write(root / "shed" / "late.md", "# Late\n" + "filler\n" * 40 + BYLINE + "\n")
    write(root / "shed" / "node_modules" / "pkg" / "readme.md", f"# Dependency\n{BYLINE}\n")
    write(root / ".hidden" / "x.md", f"# Hidden\n{BYLINE}\n")
    write(root / "venv" / "y.md", f"# Venv\n{BYLINE}\n")
    write(root / "shed" / "notes.txt", f"{BYLINE}\n")                          # discovery looks at Markdown only
    write(root / "shed" / "b.markdown", f"# Also found\n{BYLINE}\n")
    configure(home, roots=[str(root)], discover="^Prepared for ")
    out = reports.listing()
    assert sorted(titles(out)) == ["Also found", "Found"] and {r["source"] for r in out["items"]} == {"found"}


def test_a_file_both_indexed_and_found_shows_once_with_the_index_metadata(home, tmp_path):
    root = tmp_path / "root"
    write(root / "p" / "r.md", f"# From the file\n{BYLINE}\n")
    write(tmp_path / "index.json", json.dumps([{"path": "p/r.md", "title": "From the index", "by": "QA"}]))
    configure(home, index=str(tmp_path / "index.json"), roots=[str(root)], discover="Prepared for")
    (row,) = items()
    assert (row["title"], row["by"], row["source"]) == ("From the index", "QA", "index")


def test_discovery_is_cached_until_a_refresh(home, tmp_path):
    root = tmp_path / "root"
    write(root / "first.md", f"# First\n{BYLINE}\n")
    configure(home, roots=[str(root)], discover="Prepared for")
    assert titles() == ["First"]
    write(root / "second.md", f"# Second\n{BYLINE}\n")
    assert titles() == ["First"]                                              # within the cache time: not walked again
    assert sorted(titles(reports.listing(refresh=True))) == ["First", "Second"]


def test_discovery_never_reads_a_cloud_only_placeholder(home, tmp_path, monkeypatch):
    root = tmp_path / "root"
    write(root / "here.md", f"# Here\n{BYLINE}\n")
    size = write(root / "in-the-cloud.md", f"# Cloud\n{BYLINE}\n").stat().st_size     # the attribute is faked by its size
    real = reports._cloud_only
    monkeypatch.setattr(reports, "_cloud_only", lambda st: st.st_size == size or real(st))
    configure(home, roots=[str(root)], discover="Prepared for")
    assert titles() == ["Here"]


def test_other_document_types_are_listed_for_open_file_only(home, tmp_path):
    root = tmp_path / "root"
    write(root / "paper.pdf", "%PDF-1.7 not really")
    write(root / "tool.exe", "MZ")
    write(root / "run.ps1", "Write-Host hi")
    write(tmp_path / "index.json", json.dumps(["paper.pdf", "tool.exe", "run.ps1"]))
    configure(home, index=str(tmp_path / "index.json"), roots=[str(root)])
    (row,) = items()
    assert row["ext"] == "pdf" and row["viewable"] is False
    v = reports.view(row["id"])
    assert v["html"] is None and "Open file" in v["note"]


# ---------- what is never listed or served ----------
def test_paths_outside_the_roots_are_never_listed(home, tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    write(root / "ok.md", "# Inside\n")
    secret = write(outside / "secret.md", "# Secret\n")
    write(tmp_path / "index.json", json.dumps(["ok.md", "../outside/secret.md", str(secret), "..\\outside\\secret.md",
                                               "ok.md/../../outside/secret.md", "~/" + "x" * 30 + ".md"]))
    configure(home, index=str(tmp_path / "index.json"), roots=[str(root)], discover="Secret")
    out = reports.listing()
    assert titles(out) == ["Inside"]
    assert any("outside reports.roots" in w for w in out["warnings"])
    assert reports.view(reports.rid(secret.resolve())) is None                # not in the listing: no view, whatever the id


def test_nothing_outside_is_even_opened(home, tmp_path, monkeypatch):
    root, outside = tmp_path / "root", tmp_path / "outside"
    write(root / "ok.md", "# Inside\n")
    write(outside / "secret.md", "# Secret\n")
    write(tmp_path / "index.json", json.dumps(["../outside/secret.md", str(outside / "secret.md")]))
    configure(home, index=str(tmp_path / "index.json"), roots=[str(root)])
    touched = []
    real_resolve = Path.resolve

    def spy(self, *a, **k):
        touched.append(str(self))
        return real_resolve(self, *a, **k)
    monkeypatch.setattr(Path, "resolve", spy)
    reports.listing()
    assert not any("secret" in t for t in touched)


def reads(monkeypatch):
    """Every file whose contents reports reads, as its real path (a symlink counts as its target)."""
    seen, real = [], reports._head
    monkeypatch.setattr(reports, "_head", lambda p, *a: seen.append(os.path.realpath(p)) or real(p, *a))
    return seen


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks here")
def test_a_symlink_out_of_a_root_is_refused_and_never_read(home, tmp_path, monkeypatch):
    root, outside = tmp_path / "root", tmp_path / "outside"
    write(root / "ok.md", f"# Inside\n{BYLINE}\n")
    secret = write(outside / "secret.md", f"# Secret\n{BYLINE}\n")
    try:
        os.symlink(secret, root / "link.md")
        os.symlink(outside, root / "linkdir", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this account may not create symlinks (Windows without Developer Mode)")
    write(tmp_path / "index.json", json.dumps(["link.md", "linkdir/secret.md"]))
    configure(home, index=str(tmp_path / "index.json"), roots=[str(root)], discover="Prepared for")
    opened = reads(monkeypatch)
    assert titles() == ["Inside"]
    assert opened and not [p for p in opened if "outside" in p]               # the search never opened the link's target


@pytest.mark.skipif(not WIN, reason="junctions are a Windows feature")
def test_a_junction_out_of_a_root_is_refused_and_never_read(home, tmp_path, monkeypatch):
    import _winapi
    root, outside = tmp_path / "root", tmp_path / "outside"
    write(root / "ok.md", f"# Inside\n{BYLINE}\n")
    write(outside / "secret.md", f"# Secret\n{BYLINE}\n")
    _winapi.CreateJunction(str(outside), str(root / "junction"))
    write(tmp_path / "index.json", json.dumps(["junction/secret.md"]))
    configure(home, index=str(tmp_path / "index.json"), roots=[str(root)], discover="Prepared for")
    opened = reads(monkeypatch)
    assert titles() == ["Inside"]
    assert opened and not [p for p in opened if "outside" in p]


def test_the_api_takes_ids_from_the_listing_only(home, api, tmp_path):
    dump_report(home)
    secret = write(tmp_path / "outside" / "secret.md", "# Secret\n")
    for bad in ("..%2F..%2Fsecret", "zzz", "0123456789abc", "ABCDEF012345", "%2e%2e"):
        assert call("GET", f"{api}/api/reports/{bad}")[0] == 400, bad
        assert call("POST", f"{api}/api/reports/{bad}/read", {})[0] == 400, bad
    unknown = reports.rid(secret.resolve())
    assert call("GET", f"{api}/api/reports/{unknown}")[0] == 404
    assert call("POST", f"{api}/api/reports/{unknown}/read", {})[0] == 404
    assert call("POST", f"{api}/api/reports/{unknown}/open", {})[0] == 404
    assert call("POST", f"{api}/api/reports/{unknown}/delete", {})[0] == 404
    assert not (home.state / "reports_read.json").exists()


# ---------- read state ----------
def test_opening_a_report_marks_it_read_and_it_stays_read(home, api):
    dump_report(home)
    code, out = call("GET", f"{api}/api/reports")
    assert code == 200 and out["unread"] == 1
    rid = out["items"][0]["id"]
    code, doc = call("GET", f"{api}/api/reports/{rid}")
    assert code == 200 and doc["unread"] is True                              # viewing alone does not mark it; the page posts
    assert call("POST", f"{api}/api/reports/{rid}/read", {}) == (200, {"ok": True})
    marks = json.loads((home.state / "reports_read.json").read_text(encoding="utf-8"))
    assert list(marks) == [str(next(home.reports_dir.glob("*.md")).resolve())]
    reports._info.clear()
    out = call("GET", f"{api}/api/reports")[1]
    assert out["unread"] == 0 and out["items"][0]["opened_at"] == marks[list(marks)[0]]
    assert call("POST", f"{api}/api/reports/{rid}/read", {}) == (200, {"ok": True})         # twice: the first time stays
    assert json.loads((home.state / "reports_read.json").read_text(encoding="utf-8")) == marks


def test_mark_all_read(home, api):
    dump_report(home, "20261018-093000-a1b2")
    dump_report(home, "20261019-101500-c3d4", "porch")
    assert call("POST", f"{api}/api/reports/all/read", {}) == (200, {"ok": True, "marked": 2})
    assert call("GET", f"{api}/api/reports")[1]["unread"] == 0
    assert call("POST", f"{api}/api/reports/all/read", {}) == (200, {"ok": True, "marked": 0})


@pytest.mark.skipif(not WIN, reason="Windows paths are case-insensitive")
def test_read_marks_match_whatever_the_case_of_the_path(home):
    p = dump_report(home)
    (home.state / "reports_read.json").write_text(json.dumps({str(p.resolve()).upper(): "2026-10-18T10:00:00"}), encoding="utf-8")
    (row,) = items()
    assert row["unread"] is False and row["opened_at"] == "2026-10-18T10:00:00"


def test_a_broken_read_file_is_set_aside_not_trusted(home):
    dump_report(home)
    (home.state / "reports_read.json").write_text("{not json", encoding="utf-8")
    (row,) = items()
    assert row["unread"] is True
    assert reports.mark_read(row["id"]) is True
    assert (home.state / "reports_read.bad.json").read_text(encoding="utf-8") == "{not json"
    assert items()[0]["unread"] is False


# ---------- bad settings never break the tab ----------
@pytest.mark.parametrize("section, expect, problem", [
    ("a string", "is not a group of settings", True),
    ({"index": 42}, "reports.index = 42 is not a path or a list of paths", True),
    ({"roots": {"a": 1}}, "reports.roots = ", True),
    ({"roots": ["~/x", 7]}, "reports.roots = ", True),
    ({"discover": "(unclosed"}, "is not a valid pattern", True),
    ({"discover": ["x"]}, "is not a regular expression", True),
    ({"roots": [str(Path("no") / "such" / "folder")]}, "was not found", False),
    ({"discover": "x"}, "there are none", False),
])
def test_a_bad_setting_is_a_warning_not_a_crash(home, api, section, expect, problem):
    dump_report(home)
    home.config_file.write_text(json.dumps({"reports": section}), encoding="utf-8")
    home.__init__(env=home._env)                                     # no exception at import: the default is used
    assert any(expect in p for p in home.problems) is problem       # wrong kinds of value are doctor's Settings problems
    code, out = call("GET", f"{api}/api/reports")
    assert code == 200 and len(out["items"]) == 1 and any(expect in w for w in out["warnings"]), out["warnings"]
    assert call("GET", f"{api}/api/state")[0] == 200


def test_config_set_checks_the_reports_settings(home, capsys):
    from briandump import cli
    for pair, why in ((("reports.discover", "(unclosed"), "not a valid pattern"), (("reports.index", "42"), "a path or a JSON list"),
                      (("reports.roots", "~/notes"), "JSON list")):
        assert cli.main(["config", "--set", *pair]) == 2, pair
        assert why in capsys.readouterr().err
    assert not home.config_file.exists()                                      # nothing written
    assert cli.main(["config", "--set", "reports.discover", "^Prepared for ", "--set", "reports.roots", '["~/notes"]',
                     "--set", "reports.index", "~/notes/reports.json"]) == 0
    raw = json.loads(home.config_file.read_text(encoding="utf-8"))["reports"]
    assert (raw["discover"], raw["roots"], raw["index"]) == ("^Prepared for ", ["~/notes"], "~/notes/reports.json")
    assert home.reports_discover.pattern == "^Prepared for " and home.reports_roots == ["~/notes"]
    assert home.reports_index == ["~/notes/reports.json"] and home.problems == []


@pytest.mark.parametrize("content", ["{not json", '{"path": "x.md"}', "[1, 2, null]", "", "\ufeff[]", '[{"path": ["x"]}]'])
def test_a_bad_index_file_is_a_warning_not_a_crash(home, api, tmp_path, content):
    dump_report(home)
    write(tmp_path / "index.json", content)
    configure(home, index=str(tmp_path / "index.json"), roots=[str(tmp_path)])
    code, out = call("GET", f"{api}/api/reports")
    assert code == 200 and len(out["items"]) == 1


def test_a_stale_entry_in_two_index_files_is_one_warning(home, tmp_path):
    write(tmp_path / "root" / "here.md", "# Here\n")
    for name in ("a.json", "b.json"):
        write(tmp_path / name, json.dumps(["here.md", "moved-away.md", "MOVED-AWAY.md"]))
    configure(home, index=[str(tmp_path / "a.json"), str(tmp_path / "b.json")], roots=[str(tmp_path / "root")])
    out = reports.listing()
    assert titles(out) == ["Here"] and out["warnings"] == ["1 index entry points at a file that is not there"]


def test_an_index_that_is_missing_or_a_folder_is_a_warning(home, tmp_path):
    configure(home, index=[str(tmp_path / "nope.json"), str(tmp_path)], roots=[str(tmp_path)])
    out = reports.listing()
    assert out["items"] == [] and len(out["warnings"]) == 2 and "was not found" in out["warnings"][0]


# ---------- the private filter ----------
def test_the_private_filter_hides_a_private_report_and_masks_lines_in_others(home, api, tmp_path, monkeypatch):
    root = tmp_path / "root"
    write(root / "p" / "zephyr-budget.md", "# Budget\n\nnumbers\n")
    write(root / "p" / "plan.md", "# Plan\n\nPublic part.\n\nThe zephyr part.\n\n| a | b |\n|---|---|\n| zephyr | ok |\n")
    write(root / "p" / "minutes.md", "# Minutes\n\nnothing private in here\n")
    write(tmp_path / "index.json", json.dumps(["p/zephyr-budget.md", "p/plan.md", {"path": "p/minutes.md", "by": "Zephyr"}]))
    configure(home, index=str(tmp_path / "index.json"), roots=[str(root)])
    monkeypatch.setattr(home, "private_re", re.compile("zephyr", re.I))
    monkeypatch.setattr(home, "private_words", ["zephyr"])
    out = call("GET", f"{api}/api/reports")[1]
    assert "zephyr" not in json.dumps(out).lower()
    hidden = [r for r in out["items"] if r["private"]]
    assert len(hidden) == 2 and all(r["title"] == "(private)" and r["path"] == "(private)" for r in hidden)  # by path, by author
    for r in hidden:
        doc = call("GET", f"{api}/api/reports/{r['id']}")[1]
        assert doc["html"] is None and "private filter" in doc["note"]
    plan = next(r for r in out["items"] if not r["private"])
    doc = call("GET", f"{api}/api/reports/{plan['id']}")[1]
    assert "Public part." in doc["html"] and "(private)" in doc["html"] and "zephyr" not in doc["html"].lower()


# ---------- the view ----------
def test_the_view_is_escaped_html_with_a_byline_and_links_between_reports(home, api, tmp_path):
    root = tmp_path / "root"
    write(root / "p" / "other.md", "# Other\n")
    write(root / "p" / "main.md", "# Main <report>\n\n<script>alert(1)</script>\n\nSee [the other](other.md), "
                                  "[gone](gone.md), [web](https://example.com) and [up](#main-report).\n\n"
                                  "| a | b |\n|---|---|\n| 1 | 2 |\n")
    write(tmp_path / "index.json", json.dumps([{"path": "p/main.md", "date": "2026-10-19", "by": "Main"}, "p/other.md"]))
    configure(home, index=str(tmp_path / "index.json"), roots=[str(root)])
    rows = {r["title"]: r for r in items()}
    doc = call("GET", f"{api}/api/reports/{rows['Main <report>']['id']}")[1]
    h = doc["html"]
    assert h.startswith('<h1 id="r-main-report">Main &lt;report&gt;</h1><div class="byline">2026-10-19 · Main · p/main.md · modified ')
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in h and "<script" not in h
    assert f'<a href="#" data-report="{rows["Other"]["id"]}">the other</a>' in h
    assert '<span class="xref" title="gone.md">gone</span>' in h
    assert '<a href="https://example.com" rel="noreferrer">web</a>' in h and '<a href="#r-main-report">up</a>' in h
    assert "<table>" in h


def test_a_report_without_a_heading_gets_its_title_on_top(home, tmp_path):
    root = tmp_path / "root"
    write(root / "notes.md", "Just text.\n")
    write(root / "log.txt", "line <one>\nline two\n")
    write(tmp_path / "index.json", json.dumps(["notes.md", {"path": "log.txt", "title": "The log"}]))
    configure(home, index=str(tmp_path / "index.json"), roots=[str(root)])
    rows = {r["title"]: r for r in items()}
    assert reports.view(rows["notes"]["id"])["html"].startswith('<h1>notes</h1><div class="byline">')
    assert '<pre class="plain">line &lt;one&gt;\nline two\n</pre>' in reports.view(rows["The log"]["id"])["html"]


# ---------- Open file ----------
def test_open_file_is_for_the_local_page_and_only_recorded_in_the_sandbox(home, api, monkeypatch):
    p = dump_report(home)
    rid = items()[0]["id"]
    monkeypatch.setattr(os, "startfile", lambda *a: (_ for _ in ()).throw(AssertionError("opened in the sandbox")), raising=False)
    code, out = call("POST", f"{api}/api/reports/{rid}/open", {})
    assert code == 200 and out["ok"] and "sandbox" in out["msg"]
    assert [e["path"] for e in home.side_effect_log() if e["kind"] == "open_report"] == [str(p.resolve())]
    assert items()[0]["unread"] is False                                      # opening the file counts as reading it
    monkeypatch.setattr(home, "allowed", home.allowed | {"10.0.0.7"})
    monkeypatch.setattr(server.Handler, "client_ip", lambda self: "10.0.0.7")
    assert call("POST", f"{api}/api/reports/{rid}/open", {}) == (403, {"error": "local page only"})
    assert call("GET", f"{api}/api/reports")[0] == 200                        # another allowed PC may still read the list


@pytest.mark.skipif(not WIN, reason="os.startfile is Windows only")
def test_open_file_opens_the_listed_path_and_nothing_else(home, monkeypatch):
    p = dump_report(home)
    opened = []
    monkeypatch.setattr(home, "sandbox", False)
    monkeypatch.setattr(os, "startfile", opened.append)
    ok, msg = reports.open_file(items()[0]["id"])
    assert ok and opened == [str(p.resolve())]


def test_reports_obey_the_page_guards(home, api):
    dump_report(home)
    port = api.rsplit(":", 1)[1]
    rid = items()[0]["id"]
    assert call("GET", f"{api}/api/reports", headers={"Host": f"evil.example:{port}"})[0] == 403
    assert call("POST", f"{api}/api/reports/{rid}/read", {}, headers={"Origin": "https://evil.example"})[0] == 403
    assert items()[0]["unread"] is True

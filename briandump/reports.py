"""The Reports tab: the Markdown reports in your folders, newest first, with an unread dot, read inside the panel.

    from briandump import reports
    reports.listing(refresh=False) -> {"items": [row, ...], "unread": n, "warnings": [...], "configured": bool}
    reports.view(rid) -> row + {"html": str | None, "note": str | None} | None      # None = no such report in the listing
    reports.mark_read(rid) -> bool | None         reports.mark_all_read() -> int
    reports.open_file(rid) -> (ok, msg) | None    # the file in its own app (the server allows this to the local page only)

A row: {"id", "title", "date", "by", "where", "path", "source": "dump" | "index" | "found", "ext", "viewable", "unread",
"opened_at", "private"}. `path` is relative to the root or data folder it lies in; no absolute path leaves the server.

Where the list comes from (merged; a file listed twice shows once, with the index's title / date / by):
  1. BrianDump's own brain-dump reports, <data_dir>/reports/*.md: always.
  2. reports.index: a JSON file, or a list of them, holding [{"path", "title"?, "date"?, "by"?}] (a plain string is a path).
     A relative path is looked up under each of reports.roots in turn; ~ and %VARS% are expanded.
  3. reports.discover: a regular expression. Markdown files under reports.roots whose first HEAD_LINES lines match it (a
     byline your reports share, for example). The walk skips hidden folders, node_modules and virtual environments, never
     reads a cloud-only placeholder (reading it would download it), and is cached for DISCOVER_TTL seconds
     (refresh=True walks again).
Nothing is configured by default, so a new install lists its brain-dump reports only.

Safety. A file is listed only if its path lies inside the data folder or one of reports.roots both as written and once
resolved (symlinks and junctions followed), so nothing outside them is even opened, and the page asks for a report by its id
(a hash of that path) from this listing, never by path: the API cannot be pointed at any other file. Only document types
are listed, and only Markdown and text are rendered (briandump.mdhtml escapes everything). A row whose title, author or path
matches private_filter shows as "(private)" and its text is not shown on the panel (Open file still works); in any other
report a paragraph, list item, table cell or code line that matches shows as "(private)".

Read state: <state_dir>/state/reports_read.json, {path: first opened (local ISO time)}, written when the page opens a report
or Open file is used. Keys compare case-insensitively on Windows. A file that is not valid JSON is set aside as
reports_read.bad.json and a new one is started.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import re
import stat as stat_mod
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import unquote

from briandump import mdhtml
from briandump.config import CONF, replace_retry

RENDER_EXT = {".md", ".markdown", ".txt"}
DOC_EXT = RENDER_EXT | {".pdf", ".html", ".htm", ".docx", ".doc", ".odt", ".rtf", ".xlsx", ".csv", ".pptx"}
FIND_EXT = (".md", ".markdown")
SKIP_DIRS = {"node_modules", "__pycache__", "venv", "env", "site-packages"}
HEAD_BYTES, HEAD_LINES = 4096, 20
MAX_RENDER = 2_000_000                     # bytes: a bigger file is offered through Open file only
MAX_INDEX = 20_000_000                     # bytes of one index file
MAX_ENTRIES = 5000                         # entries read from one index file
DISCOVER_TTL = 300
WALK_FILES, WALK_SECONDS = 200_000, 20.0   # a root bigger than this stops the walk with a warning
ID_RE = re.compile(r"^[0-9a-f]{12}\Z")
DATE_RE = re.compile(r"(20\d\d-[01]\d-[0-3]\d)")
DUMP_NAME_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})-\d{6}-[0-9a-f]{4}")
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
CLOUD_ONLY = 0x00400000 | 0x00040000 | 0x00001000   # FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN | OFFLINE
NO_WINDOW = 0x08000000

_read_lock = threading.Lock()              # reports_read.json: read-modify-write
_walk_lock = threading.Lock()              # one discovery walk at a time
_found = {"key": None, "t": 0.0, "paths": [], "warnings": []}
_info = {}                                 # normcase(path) -> ((mtime_ns, size), {"title", "date"})


class Busy(RuntimeError):
    """reports_read.json stayed locked by another program: the server answers 503 and the page tries again."""


# ---------- settings ----------
def _expand(value):
    return Path(os.path.expandvars(os.path.expanduser(str(value).strip().strip('"').replace("\\", "/"))))


def _norm(p):
    """A path compared as a string: absolute, '..' folded, case-folded on Windows."""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(p))))


def settings():
    """The reports settings checked against the disk -> (index files, roots, discover regex or None, warnings). A value of
    the wrong kind is already a config problem (CONF.problems: doctor's Settings row) and arrives here empty; it is repeated
    on the tab. A root that is missing is a warning here. Nothing in here stops the panel. Roots are (as written, resolved)
    pairs of folders."""
    warn = [p for p in CONF.problems if p.startswith("reports")]
    index = [_expand(v) for v in CONF.reports_index]
    roots = []
    for v in CONF.reports_roots:
        p = _expand(v)
        if not p.is_absolute():
            p = Path.home() / p
        try:
            real = p.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            warn.append(f"reports.roots: {v} was not found")
            continue
        if not real.is_dir():
            warn.append(f"reports.roots: {v} is not a folder")
            continue
        roots.append((Path(os.path.normpath(os.path.abspath(p))), real))
    rx = CONF.reports_discover
    if rx is not None and not roots:
        warn.append("reports.discover searches reports.roots, and there are none")
    return index, roots, rx, warn


def _bases(roots):
    """Every folder a listed file may lie in, as (written, resolved) pairs: the data folder first, then the roots."""
    data = Path(os.path.normpath(os.path.abspath(CONF.data_dir)))
    try:
        real = data.resolve()
    except (OSError, RuntimeError):
        real = data
    return [(data, real)] + list(roots)


def _within(p, folders):
    n = _norm(p)
    for f in folders:
        base = _norm(f)
        if n == base or n.startswith(base.rstrip(os.sep) + os.sep):
            return True
    return False


def _lexical(bases):
    """Both spellings of every base (as written and resolved): a path is looked at only if it lies in one of them."""
    return [w for w, _r in bases] + [r for _w, r in bases]


def _check(path, bases):
    """The real file behind `path`, if the path as written lies inside one of `bases` and the resolved one does too."""
    if not _within(path, _lexical(bases)):
        return None
    try:
        real = Path(path).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not real.is_file() or not _within(real, [r for _w, r in bases]):
        return None
    return real


def resolve(ref, roots, bases):
    """An index entry's path -> (real file | None, why: None | "outside" | "missing")."""
    s = str(ref or "").strip()
    if not s or "\x00" in s or len(s) > 1000:
        return None, "missing"
    p = _expand(s)
    cands = [p] if p.is_absolute() else [w / p for w, _r in roots]
    inside = [c for c in cands if _within(c, _lexical(bases))]
    if not inside:
        return None, "outside"
    for c in inside:
        real = _check(c, bases)
        if real:
            return real, None
    return None, "missing"


# ---------- the sources ----------
def _dump_reports(bases):
    try:
        files = sorted(CONF.reports_dir.glob("*.md"))
    except OSError:
        return []
    return [r for r in (_check(f, bases[:1]) for f in files) if r]


def _read_index(path):
    """One index file -> (entries, warning or None). Entries: {"path", "title"?, "date"?, "by"?, "project"?} strings."""
    try:
        if path.stat().st_size > MAX_INDEX:
            return [], f"reports.index: {path} is larger than {MAX_INDEX // 1_000_000} MB"
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return [], f"reports.index: {path} was not found"
    except (OSError, UnicodeError) as e:
        return [], f"reports.index: {path} could not be read ({getattr(e, 'strerror', None) or e})"
    try:
        data = json.loads(raw)
    except ValueError as e:
        return [], f"reports.index: {path} is not valid JSON ({e})"
    if not isinstance(data, list):
        return [], f'reports.index: {path} must hold a list, like [{{"path": "notes/plan.md"}}]'
    out = []
    for e in data[:MAX_ENTRIES]:
        if isinstance(e, str):
            e = {"path": e}
        if not isinstance(e, dict) or not isinstance(e.get("path"), str) or not e["path"].strip():
            continue
        row = {"path": e["path"]}
        for k in ("title", "date", "by", "project"):
            v = e.get(k)
            if isinstance(v, (str, int, float)) and not isinstance(v, bool) and str(v).strip():
                row[k] = " ".join(str(v).split())[:300]
        out.append(row)
    return out, None


def _cloud_only(st):
    return bool(getattr(st, "st_file_attributes", 0) & CLOUD_ONLY)


def _is_link(path):
    """A symlink or a junction (Windows' folder link): never walked into (its target may lie anywhere)."""
    try:
        st = os.lstat(path)
    except OSError:
        return True
    if stat_mod.S_ISLNK(st.st_mode):
        return True
    tag = getattr(st, "st_reparse_tag", 0)
    return bool(tag) and tag == getattr(stat_mod, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


def _head(path, nbytes=HEAD_BYTES):
    """The start of a text file, decoded; None for a cloud-only placeholder or a file that cannot be read."""
    try:
        if _cloud_only(os.stat(path)):
            return None
        with open(path, "rb") as f:
            raw = f.read(nbytes)
    except OSError:
        return None
    return raw.decode("utf-8", "replace").lstrip("\ufeff")


def _walk(roots, rx):
    found, warn, seen, t0 = [], [], 0, time.monotonic()
    for written, _real in roots:
        for dirpath, dirs, files in os.walk(written):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d.lower() not in SKIP_DIRS
                             and not _is_link(os.path.join(dirpath, d)))
            for name in sorted(files):
                seen += 1
                if seen > WALK_FILES or time.monotonic() - t0 > WALK_SECONDS:
                    warn.append(f"reports.discover stopped after {seen - 1} files: make reports.roots narrower")
                    return found, warn
                if not name.lower().endswith(FIND_EXT):
                    continue
                ok = _check(os.path.join(dirpath, name), roots)    # first: a symlinked file may point anywhere
                if not ok:
                    continue
                head = _head(ok)
                if head is not None and rx.search("\n".join(head.split("\n")[:HEAD_LINES])):
                    found.append(ok)
    return found, warn


def discovered(roots, rx, refresh=False):
    """Files under the roots whose first lines match `rx` (cached DISCOVER_TTL seconds) -> (paths, warnings)."""
    if rx is None or not roots:
        return [], []
    key = (tuple(str(w) for w, _r in roots), rx.pattern)
    with _walk_lock:
        if not refresh and _found["key"] == key and time.monotonic() - _found["t"] < DISCOVER_TTL:
            return _found["paths"], _found["warnings"]
        paths, warn = _walk(roots, rx)
        _found.update(key=key, t=time.monotonic(), paths=paths, warnings=warn)
        return paths, warn


# ---------- one file ----------
def rid(path):
    """A report's id: 12 hex digits of the hash of its resolved path (case-folded on Windows)."""
    return hashlib.sha1(os.path.normcase(str(path)).encode("utf-8", "surrogatepass")).hexdigest()[:12]


def _file_info(p, st):
    """Title and date from the file (cached until it changes): the first '# ' heading, else the file name; a date in the
    file name, else the first date in its opening lines, else the day it was last modified."""
    key = os.path.normcase(str(p))
    hit = _info.get(key)
    if hit and hit[0] == (st.st_mtime_ns, st.st_size):
        return hit[1]
    title = head_date = None
    if p.suffix.lower() in RENDER_EXT:
        head = _head(p, 8192) or ""
        if p.suffix.lower() != ".txt":
            title = mdhtml.first_heading(head)
        m = DATE_RE.search(head[:1500])
        head_date = m.group(1) if m else None
    m = DATE_RE.search(p.name) or None
    d = DUMP_NAME_RE.match(p.name)
    date = (m.group(1) if m else None) or (f"{d.group(1)}-{d.group(2)}-{d.group(3)}" if d else None) or head_date
    info = {"title": (title or p.stem)[:200], "date": date or dt.date.fromtimestamp(st.st_mtime).isoformat()}
    if len(_info) > 5000:
        _info.clear()
    _info[key] = ((st.st_mtime_ns, st.st_size), info)
    return info


def _place(p, bases):
    """(where, path): the project folder a file sits in and its path below its root ('' where for brain dumps)."""
    for i, (_written, real) in enumerate(bases):
        try:
            rel = p.relative_to(real)
        except ValueError:
            continue
        if i == 0:
            return "", rel.as_posix()
        return (rel.parts[0] if len(rel.parts) > 1 else real.name), rel.as_posix()
    return "", p.name


def _private(*texts):
    return any(t and CONF.private_re.search(t) for t in texts)


def _mask(t):
    return "(private)" if _private(t) else t


def _valid_date(v):
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", str(v or ""))
    return m.group(1) if m else None


# ---------- read state ----------
def _read_file():
    return CONF.reports_read


def read_marks():
    """{normcase(path): first opened} from the read-state file ({} when it is missing, locked or broken)."""
    try:
        raw = json.loads(_read_file().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return {}
    return {os.path.normcase(str(k)): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def _mark(paths):
    """Add these paths to the read-state file (first open wins) -> how many were new. Busy if it stays locked."""
    f = _read_file()
    with _read_lock:
        try:
            raw = json.loads(f.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            raw = {}
        except (ValueError, UnicodeError):
            raw = None
        except OSError as e:
            raise Busy("the read-state file is locked by another program; try again") from e
        if not isinstance(raw, dict):                     # not ours to read: keep it aside, start a new one
            try:
                replace_retry(f, f.with_name("reports_read.bad.json"))
            except OSError:
                pass
            raw = {}
        known = {os.path.normcase(str(k)) for k in raw}
        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        new = 0
        for p in paths:
            if os.path.normcase(str(p)) not in known:
                raw[str(p)] = now
                known.add(os.path.normcase(str(p)))
                new += 1
        if new:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_name(f"{f.name}.{os.getpid()}-{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(raw, indent=1, ensure_ascii=False), encoding="utf-8")
            try:
                replace_retry(tmp, f)
            except OSError as e:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise Busy("the read-state file stayed locked by another program; try again") from e
        return new


# ---------- the listing ----------
PUBLIC = ("id", "title", "date", "by", "where", "path", "source", "ext", "viewable", "unread", "opened_at", "private")


def _rows(refresh=False):
    """-> (rows newest first, warnings, configured). Rows carry the real path in "_p" (never sent to the page)."""
    index, roots, rx, warn = settings()
    bases = _bases(roots)
    entries = [(p, {}, "dump") for p in _dump_reports(bases)]
    skipped = {"outside": set(), "missing": set()}         # paths, so one stale entry in two index files counts once
    for f in index:
        items, w = _read_index(f)
        if w:
            warn.append(w)
        for e in items:
            p, why = resolve(e["path"], roots, bases)
            if p is None:
                skipped[why].add(e["path"].strip().replace("\\", "/").lower())
                continue
            entries.append((p, e, "index"))
    n = len(skipped["outside"])
    if n:
        warn.append(f"{n} index entr{'y is' if n == 1 else 'ies are'} outside reports.roots and the data folder, so not listed")
    n = len(skipped["missing"])
    if n:
        warn.append(f"{n} index entr{'y points' if n == 1 else 'ies point'} at a file that is not there")
    found, w = discovered(roots, rx, refresh)
    warn += w
    entries += [(p, {}, "found") for p in found]
    marks, rows, seen = read_marks(), [], set()
    for p, meta, source in entries:
        key = os.path.normcase(str(p))
        if key in seen or p.suffix.lower() not in DOC_EXT:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        seen.add(key)
        info = _file_info(p, st)
        title = meta.get("title") or info["title"]
        by = meta.get("by") or ("BrianDump" if source == "dump" else "")
        where, rel = _place(p, bases)
        where = meta.get("project") or where
        private = _private(title, by, rel, where)
        opened = marks.get(key)
        rows.append({"id": rid(p), "title": "(private)" if private else title,
                     "date": _valid_date(meta.get("date")) or info["date"], "by": _mask(by), "where": _mask(where),
                     "path": "(private)" if private else rel, "source": source, "ext": p.suffix.lower().lstrip("."),
                     "viewable": p.suffix.lower() in RENDER_EXT, "unread": opened is None, "opened_at": opened,
                     "private": private, "_p": p, "_m": st.st_mtime})
    rows.sort(key=lambda r: (r["date"], r["_m"]), reverse=True)
    return rows, warn, bool(index or roots or rx)


def _public(row):
    return {k: row[k] for k in PUBLIC}


def listing(refresh=False):
    rows, warn, configured = _rows(refresh)
    return {"items": [_public(r) for r in rows], "unread": sum(r["unread"] for r in rows), "warnings": warn,
            "configured": configured}


def _find(report_id):
    if not ID_RE.match(str(report_id or "")):
        return None, []
    rows = _rows()[0]
    return next((r for r in rows if r["id"] == report_id), None), rows


# ---------- one report ----------
def _linker(src, rows):
    """How a link inside a report is shown: http(s) and #anchors as they are, a relative link to another listed report
    opens that report in the panel, anything else is text."""
    by_path = {_norm(r["_p"]): r["id"] for r in rows}

    def link(href):
        h = href.strip()
        if mdhtml.SAFE_URL_RE.match(h):
            return ("href", h)
        if h.startswith("#"):
            return ("anchor", h[1:])
        if h.startswith("//") or (SCHEME_RE.match(h) and not DRIVE_RE.match(h)):
            return None
        target = unquote(h.split("#", 1)[0].split("?", 1)[0]).replace("\\", "/")
        if not target:
            return None
        p = Path(target) if Path(target).is_absolute() else src.parent / target
        found = by_path.get(_norm(p))
        return ("report", found) if found else None
    return link


def _with_byline(body, row, st):
    bits = [row["date"], row["by"], row["path"], "modified " + dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")]
    byline = '<div class="byline">' + " · ".join(html.escape(b) for b in bits if b) + "</div>"
    m = re.match(r"\s*<h1\b[^>]*>.*?</h1>", body, re.S)
    if m:
        return body[:m.end()] + byline + body[m.end():]
    return f"<h1>{html.escape(row['title'])}</h1>{byline}{body}"


def view(report_id):
    """The report rendered for the panel (None when the id is not in the listing). Private rows and other file types come
    back with a note instead of html."""
    row, rows = _find(report_id)
    if row is None:
        return None
    out = _public(row)
    out.update(html=None, note=None)
    p = row["_p"]
    if row["private"]:
        out["note"] = "This report matches your private filter, so the panel does not show it. Open file opens it in its own app."
        return out
    if not row["viewable"]:
        out["note"] = f"A .{row['ext']} file: Open file opens it in its own app."
        return out
    try:
        st = p.stat()
        if st.st_size > MAX_RENDER:
            out["note"] = f"Too large to show here ({st.st_size / 1e6:.1f} MB). Open file opens it in its own app."
            return out
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        out["note"] = f"Could not read the file ({e.strerror or e})."
        return out
    private = CONF.private_re if CONF.private_words else None
    if p.suffix.lower() == ".txt":
        lines = text.replace("\r\n", "\n").split("\n")
        body = '<pre class="plain">' + html.escape("\n".join(
            "(private)" if private and private.search(ln) else ln for ln in lines)) + "</pre>"
    else:
        body = mdhtml.render(text, link=_linker(p, rows), private=private, id_prefix="r-")
    out["html"] = _with_byline(body, row, st)
    return out


def mark_read(report_id):
    """Mark one report read -> True, or None when the id is not in the listing."""
    row, _rows_ = _find(report_id)
    if row is None:
        return None
    _mark([row["_p"]])
    return True


def mark_all_read():
    """Mark every listed report read -> how many were unread."""
    return _mark([r["_p"] for r in _rows()[0] if r["unread"]])


def open_file(report_id):
    """Open a listed report in its own app (Windows' default for its type, else Explorer with it selected) and mark it
    read -> (ok, msg), or None when the id is not in the listing. Sandbox: recorded, nothing opened."""
    row, _rows_ = _find(report_id)
    if row is None:
        return None
    p = row["_p"]
    if CONF.record("open_report", path=str(p)):
        _mark([p])
        return True, f"sandbox: would open {p.name}"
    if os.name != "nt":
        return False, "opening files needs Windows"
    try:
        os.startfile(str(p))                              # a listed file's own path, never one from the page
        msg = f"opened {p.name}"
    except OSError:                                       # no app for this type: show it in Explorer instead
        try:
            subprocess.Popen(["explorer.exe", f"/select,{p}"], creationflags=NO_WINDOW)
        except OSError as e:
            return False, f"could not open {p.name} ({e.strerror or e})"
        msg = f"no app opens .{row['ext']} files here: showing {p.name} in Explorer"
    _mark([p])
    return True, msg

"""agenda.md and questions.md: plain markdown checklists that stay yours.

    - [ ] an open item
    - [x] a done one ([X] works too)

    from briandump import agenda
    agenda.add(CONF.agenda_file, ["Call the lumber yard"], suffix="(from brain dump 2026-10-18 10:42)")
    agenda.read(CONF.agenda_file)            # [{"text", "done", "line", "base"}]
    agenda.set_done(CONF.agenda_file, "Call the lumber yard")

Why markdown and not JSON: the files live in your data folder, so you can open them in any editor, tick things off by
hand, keep them in a notes app, or point `agenda.file` at a bigger notes file and scope BrianDump to one `## heading`.
BrianDump only touches the lines it adds or ticks. Every other byte stays as it was: line endings, a byte-order mark,
text that is not valid UTF-8, fenced code blocks (whose lines are never read as items or headings).

Dedupe: a new item is compared with the OPEN items in scope by norm(), which ignores case, punctuation, spacing and the
"(from brain dump ...)" note BrianDump appends (so a short item added with that note still matches the same words later).
Two items are the same when their normalised text is equal, or when both are longer than 20 characters and one contains
the other as whole words ("call the lumber yard tomorrow" vs "call the lumber yard tomorrow morning"). A done item does
not block adding the same thing again: a chore you mention next week comes back.

Writes go to a temp file that then replaces the original, so a crash never leaves half a file. On Windows os.replace
fails while another process has the file open (an editor, a sync client, the panel reading it), so it is retried for
about a second before giving up; the original is never truncated. If the file changed between our read and our write
(you saved it in an editor), the edit is redone on the new content.
"""
from __future__ import annotations

import os
import re
import threading
import time
import unicodedata
from pathlib import Path

ITEM_RE = re.compile(r"^(?P<lead>[ \t]*[-*+][ \t]+\[)(?P<mark>[ xX])(?P<close>\][ \t]+)(?P<text>\S.*?)[ \t]*$")
HEAD_RE = re.compile(r"^ {0,3}(?P<hashes>#{1,6})[ \t]+(?P<title>.*?)(?:[ \t]+#+)?[ \t]*$")
FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})")
BLOCK_RE = re.compile(r"^ {0,3}(?:[-*+][ \t]|\d{1,9}[.)][ \t]|#{1,6}[ \t]|>|`{3,}|~{3,})")   # a line that starts a new block
NOTE_RE = re.compile(r"\s*\(\s*from\b[^()]*\bdump\b[^()]*\)\s*$", re.I)                     # "(from brain dump 10-18 10:42)"
PREFIX_RE = re.compile(r"^\s*(?:[-*+]\s+)?(?:\[[ xX]\]\s+)?")                                # a pasted "- [ ] " in front
CONTAIN_MIN = 20
REPLACE_TRIES, REPLACE_PAUSE_S = 20, 0.04
HEADERS = {
    "agenda": "# Agenda\n\nOne line per item: - [ ] open, - [x] done. BrianDump adds brain-dump to-dos here.\n\n",
    "questions": "# Questions\n\nOne line per question: - [ ] open, - [x] answered. BrianDump adds the questions from your brain dumps "
                 "here.\n\n",
}
_LOCK = threading.RLock()


# ---------- normalising ----------
def strip_note(text):
    """The item text without the "(from brain dump ...)" note BrianDump appends."""
    return NOTE_RE.sub("", str(text or "")).strip()


def norm(text):
    """Comparable form: no note, case-folded, apostrophes dropped, other punctuation and spacing collapsed to one space."""
    s = unicodedata.normalize("NFKC", strip_note(text)).casefold()
    s = re.sub(r"['‘’`]", "", s)
    return " ".join(re.sub(r"[\W_]+", " ", s).split())


def same(a, b):
    """The dedupe rule on two norm()ed strings: equal, or both longer than 20 characters and one holds the other."""
    if not a or not b:
        return False
    if a == b:
        return True
    return len(a) > CONTAIN_MIN and len(b) > CONTAIN_MIN and (f" {a} " in f" {b} " or f" {b} " in f" {a} ")


def clean(text):
    """One line of item text: whitespace (newlines too) collapsed, a pasted '- [ ] ' prefix removed."""
    return PREFIX_RE.sub("", " ".join(str(text or "").split()), count=1).strip()


# ---------- reading ----------
def _split(text):
    """Lines without their endings; a byte-order mark on the first line is not part of it."""
    lines = str(text).splitlines()
    if lines and lines[0].startswith("﻿"):
        lines[0] = lines[0][1:]
    return lines


def _walk(lines):
    """(index, heading level, heading title, item match) for every line outside fenced code."""
    fence = None
    for n, line in enumerate(lines):
        f = FENCE_RE.match(line)
        if fence:
            if f and f["fence"][0] == fence[0] and len(f["fence"]) >= len(fence) and not line.strip().strip(fence[0]):
                fence = None
            continue
        if f:
            fence = f["fence"]
            continue
        h = HEAD_RE.match(line)
        if h:
            yield n, len(h["hashes"]), h["title"], None
        else:
            yield n, None, None, ITEM_RE.match(line)


def _scoped(lines, heading=None):
    """_walk plus `inside`: the line sits under a heading titled `heading` (any level, until the next heading of the same
    level or higher). Always True when heading is None."""
    want = norm(heading) if heading else None
    inside, level = want is None, 0
    for n, hl, title, m in _walk(lines):
        if hl is not None and want is not None:
            if inside and hl <= level:
                inside = False
            if not inside and norm(title) == want:
                inside, level = True, hl
        yield n, hl, title, m, inside


def parse(text, heading=None):
    """-> [{"text", "done", "line", "base"}] for every checklist item (in scope). `line` is the 0-based line index; `base`
    is the text without BrianDump's "(from brain dump ...)" note."""
    out = []
    for n, _hl, _title, m, inside in _scoped(_split(text), heading):
        if m and inside:
            out.append({"text": m["text"], "done": m["mark"] != " ", "line": n, "base": strip_note(m["text"])})
    return out


def read(path, heading=None):
    """The items of a checklist file; [] when it does not exist yet."""
    try:
        return parse(read_text_retry(path, errors="replace"), heading)
    except OSError:
        return []


def open_items(path, heading=None):
    return [i["text"] for i in read(path, heading) if not i["done"]]


# ---------- writing ----------
def replace_retry(src, dst):
    """os.replace, retried while Windows refuses because another handle has `dst` open (WinError 5 / 32)."""
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt >= REPLACE_TRIES - 1:
                raise
            time.sleep(REPLACE_PAUSE_S)


def write_text_atomic(path, text, errors="strict"):
    """Write UTF-8 `text` (newlines exactly as given) to a temp file beside `path`, then swap it in. On failure the temp
    file is removed and the original is untouched. dumps.py uses this for meta.json too."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", errors=errors, newline="") as f:
            f.write(text)
        replace_retry(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def read_text_retry(path, errors="strict", tries=10, pause=0.02):
    """read_text that rides out the moment another process is swapping the file in (a transient PermissionError)."""
    for attempt in range(tries):
        try:
            with open(path, encoding="utf-8", errors=errors, newline="") as f:
                return f.read()
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(pause)


def _raw(path):
    """(exists, text) with line endings untouched and undecodable bytes kept as surrogates (written back as they were)."""
    try:
        return True, read_text_retry(path, errors="surrogateescape")
    except FileNotFoundError:
        return False, ""


def _header(path):
    return HEADERS["questions"] if "question" in Path(path).name.lower() else HEADERS["agenda"]


def _indent(line):
    return len(line) - len(line.lstrip(" \t"))


def _after_item(lines, i, end):
    """Index just past item i and the lines that belong to it (indented children, lazy continuation lines)."""
    base, j = _indent(lines[i]), i + 1
    while j < end:
        if lines[j].strip():
            if _indent(lines[j]) > base or not BLOCK_RE.match(lines[j]):
                j += 1
                continue
            break
        k = j
        while k < end and not lines[k].strip():
            k += 1
        if k < end and _indent(lines[k]) > base:          # a blank line inside the item: its next paragraph is indented
            j = k
            continue
        break
    return j


def _section(lines, heading):
    """(heading line, end) of the first section titled `heading`; None when the file has no such heading."""
    want, start, level = norm(heading), None, 0
    for n, hl, title, _m in _walk(lines):
        if hl is None:
            continue
        if start is not None and hl <= level:
            return start, n
        if start is None and norm(title) == want:
            start, level = n, hl
    return (start, len(lines)) if start is not None else None


def _insert(text, new_lines, heading=None):
    """`text` with `new_lines` placed after the last item of the scope (or appended), everything else byte-for-byte."""
    nl = "\r\n" if "\r\n" in text else "\n"
    raw, lines = text.splitlines(keepends=True), _split(text)
    block = [ln + nl for ln in new_lines]
    lo, hi, add_heading = 0, len(lines), False
    if heading:
        sec = _section(lines, heading)
        if sec is None:
            add_heading = True
        else:
            lo, hi = sec
    at, gap = None, False
    if not add_heading:
        items = [n for n, hl, _t, m in _walk(lines) if m and lo <= n < hi]
        if items:
            at = _after_item(lines, items[-1], hi)
        else:
            filled = [n for n in range(lo, hi) if lines[n].strip()]
            if heading or filled:
                last = filled[-1] if filled else lo
                at, gap = last + 1, True                    # after the heading or the section's text, one blank line between
    if at is None:                                          # append at the end of the file
        at, gap = len(raw), bool(lines) and bool(lines[-1].strip())
        if add_heading:
            block = [f"## {heading}{nl}", nl] + block
    if at > 0 and raw[at - 1].splitlines() == [raw[at - 1]]:     # the line before has no line ending (end of file)
        raw[at - 1] += nl
    if gap:
        block = [nl] + block
    return "".join(raw[:at] + block + raw[at:])


def _commit(path, existed, original, new_text):
    """Write new_text unless the file changed since it was read (then the caller redoes its edit). True = written."""
    if _raw(path) != (existed, original):
        return False
    write_text_atomic(path, new_text, errors="surrogateescape")
    return True


def add(path, texts, *, heading=None, suffix=None):
    """Append the new ones of `texts` as '- [ ] text' lines (plus ' ' + suffix) and return them (cleaned, without the
    suffix). Dedupe against the open items in scope and within `texts`. A missing file is created with a short header;
    with `heading` the items go under that '## heading' (created at the end of the file if it is not there)."""
    if isinstance(texts, str):
        texts = [texts]
    suffix = " ".join(str(suffix).split()) if suffix else ""
    with _LOCK:
        for _ in range(5):
            existed, original = _raw(path)
            text = original if existed else _header(path)
            have = [norm(_unsuffix(i["text"], suffix)) for i in parse(text, heading) if not i["done"]]
            added = []
            for t in texts:
                t = clean(t)
                n = norm(t)
                if not n or any(same(n, h) for h in have):
                    continue
                have.append(n)
                added.append(t)
            if not added:
                return []
            new = [f"- [ ] {t}" + (f" {suffix}" if suffix else "") for t in added]
            if existed:
                new_text = _insert(original, new, heading)
            else:
                new_text = text + (f"## {heading}\n\n" if heading else "") + "".join(f"{ln}\n" for ln in new)
            if _commit(path, existed, original, new_text):
                return added
    raise RuntimeError(f"{path} kept changing while BrianDump tried to add to it; try again")


def _unsuffix(text, suffix):
    return text[: -len(suffix)].rstrip() if suffix and text.endswith(suffix) else text


def set_done(path, text, done=True, *, heading=None):
    """Tick (or untick) the item whose norm() equals norm(text): an item in the other state is preferred, so ticking finds
    the open copy. True when such an item exists (already in that state counts); only its checkbox character changes."""
    want = norm(text)
    if not want:
        return False
    with _LOCK:
        for _ in range(5):
            existed, original = _raw(path)
            if not existed:
                return False
            hits = [i for i in parse(original, heading) if norm(i["text"]) == want]
            if not hits:
                return False
            target = next((i for i in hits if i["done"] != bool(done)), None)
            if target is None:
                return True
            raw = original.splitlines(keepends=True)
            raw[target["line"]] = _mark(raw[target["line"]], done)
            if _commit(path, existed, original, "".join(raw)):
                return True
    raise RuntimeError(f"{path} kept changing while BrianDump tried to tick an item; try again")


def _mark(line, done):
    """The same line with its checkbox set to [x] or [ ] (indentation, bullet, text and line ending untouched)."""
    bom = "﻿" if line.startswith("﻿") else ""
    body = line[len(bom):]
    m = ITEM_RE.match(body.rstrip("\r\n"))
    if not m:
        return line
    pos = m.start("mark")
    return bom + body[:pos] + ("x" if done else " ") + body[pos + 1:]

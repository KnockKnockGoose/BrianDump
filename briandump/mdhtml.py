"""A small Markdown -> HTML renderer for the Reports tab: the CommonMark / GitHub subset that reports are written in.

    from briandump import mdhtml
    mdhtml.render(text, link=None, private=None, id_prefix="") -> str     # an HTML fragment
    mdhtml.first_heading(text) -> str | None                              # the first "# " heading, markup removed

Covered: ATX headings (# ... ######), paragraphs, hard line breaks, **bold**, *italic*, ***both***, ~~strike~~, `code`,
fenced code (``` or ~~~), [links](url "title"), <autolinks> and bare http(s) URLs, images (shown as their alt text),
bullet / numbered / task lists (nested), blockquotes, tables with column alignment, horizontal rules, backslash escapes, and
YAML front matter (dropped). Not covered: setext headings, indented code blocks, reference-style links, footnotes, raw HTML.

Safety: raw HTML never passes through. Every character of the source reaches the page escaped, and the only tags in the
output are the ones written here (plus <br> for a literal <br> in the source, which tables often hold). A link keeps its
target only when it is http(s), an in-page #anchor, or what `link(href)` maps it to; anything else (javascript:, data:,
file:, a relative path nobody resolved) becomes plain text with the target as its tooltip.

Speed: emphasis spans are at most 1000 characters (so each pattern runs in linear time), a block longer than MAX_INLINE
characters and every block after BUDGET_S seconds of formatting are shown escaped without inline formatting, and lists or
quotes nested deeper than MAX_DEPTH are plain text: no file can keep the server busy for long.

`link(href)` -> ("href", url) | ("anchor", fragment) | ("report", id) | None: how the caller wants a link target shown.
`private`: a compiled regex. A heading, paragraph, list item text, table cell or code line that matches is shown as
'(private)', and the anchor id of a masked heading says nothing about its text.
`id_prefix`: put in front of every heading id (and every #anchor link), so ids never clash with the page around them.
"""
from __future__ import annotations

import html
import re
import time

MAX_INLINE = 20_000
MAX_DEPTH = 12
BUDGET_S = 3.0
PRIVATE = "(private)"

FRONT_RE = re.compile(r"\A---[ \t]*\n.*?\n(?:---|\.\.\.)[ \t]*(?:\n|\Z)", re.S)
FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
HR_RE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
QUOTE_RE = re.compile(r"^ {0,3}> ?(.*)$")
LIST_RE = re.compile(r"^( *)([-*+]|\d{1,9}[.)])(?:([ \t]+)(.*))?$")
TASK_RE = re.compile(r"^\[([ xX])\](?:[ \t]+|$)")
DELIM_ROW_RE = re.compile(r"^ {0,3}\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")
CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")

# inline (applied to the raw text of one block, in this order)
CODE_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.S)
ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")
BREAK_RE = re.compile(r"(?: {2,}|\\)\n")
BR_TAG_RE = re.compile(r"<br\s*/?>", re.I)
_TEXT = r"((?:[^\[\]]|\[[^\[\]]*\])*)"                               # link text: one level of nested brackets
_DEST = r"(<[^<>\n]*>|(?:[^\s()]|\((?:[^\s()])*\))*)"                 # <dest with spaces> or a run with balanced parens
_TITLE = r"(?:\s+(\"[^\"\n]*\"|'[^'\n]*'))?"
IMAGE_RE = re.compile(r"!\[" + _TEXT + r"\]\(\s*" + _DEST + _TITLE + r"\s*\)")
LINK_RE = re.compile(r"\[" + _TEXT + r"\]\(\s*" + _DEST + _TITLE + r"\s*\)")
AUTOLINK_RE = re.compile(r"<(https?://[^\s<>]+)>", re.I)
BARE_URL_RE = re.compile(r"(?<![\w/@&=.])https?://[^\s<>\"'`\x00]+", re.I)
SAFE_URL_RE = re.compile(r"^https?://[^\s\"'<>\\\x00-\x1f\x7f]+$", re.I)
_SPAN = r"([\s\S]{1,1000}?)"                                            # an emphasis span: at most 1000 characters
EMPHASIS = [(re.compile(rx.replace("SPAN", _SPAN)), rep) for rx, rep in (
    (r"\*\*\*(?=\S)SPAN(?<=\S)\*\*\*", r"<strong><em>\1</em></strong>"),
    (r"\*\*(?=\S)SPAN(?<=\S)\*\*", r"<strong>\1</strong>"),
    (r"(?<![\w\\])__(?=\S)SPAN(?<=\S)__(?!\w)", r"<strong>\1</strong>"),
    (r"(?<![\w*])\*(?=[^\s*])SPAN(?<=[^\s*])\*(?![\w*])", r"<em>\1</em>"),
    (r"(?<![\w_])_(?=[^\s_])SPAN(?<=[^\s_])_(?![\w_])", r"<em>\1</em>"),
    (r"~~(?=\S)SPAN(?<=\S)~~", r"<del>\1</del>"),
)]
PH_RE = re.compile(r"\x00(\d+)\x00")


def default_link(href):
    """No resolver: http(s) links and #anchors keep their target, everything else is text."""
    if SAFE_URL_RE.match(href):
        return ("href", href)
    if href.startswith("#") and len(href) > 1:
        return ("anchor", href[1:])
    return None


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _expand(line):
    """Tabs in the leading whitespace count as four spaces (list nesting and fences are measured in spaces)."""
    m = re.match(r"[ \t]+", line)
    return m.group(0).expandtabs(4) + line[m.end():] if m and "\t" in m.group(0) else line


def split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in CELL_SPLIT_RE.split(s)]     # GFM: \| is a pipe, in code spans too


def _align(cell):
    c = cell.strip()
    if c.startswith(":") and c.endswith(":"):
        return ' class="al-c"'
    if c.endswith(":"):
        return ' class="al-r"'
    return ""


def first_heading(text):
    """The text of the first level-1 heading, without inline markup (None if there is none outside code fences)."""
    fence = None
    for line in str(text or "").replace("\r\n", "\n").split("\n"):
        m = FENCE_RE.match(line)
        if m and (fence is None or m.group(2)[0] == fence):
            fence = None if fence else m.group(2)[0]
            continue
        if fence:
            continue
        m = HEADING_RE.match(line)
        if m and len(m.group(1)) == 1 and (m.group(2) or "").strip():
            return plain_text(m.group(2))
    return None


def plain_text(md):
    """Inline markdown -> its text: [text](url) -> text, `code` -> code, emphasis marks and backslashes dropped."""
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", str(md)[:2000])
    s = re.sub(r"<(https?://[^>]+)>", r"\1", s)
    s = re.sub(r"(\*\*|__|~~)(?=\S)(.+?)(?<=\S)\1", r"\2", s)
    s = re.sub(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])", r"\1", s)
    s = re.sub(r"(?<![\w_])_(?=\S)(.+?)(?<=\S)_(?![\w_])", r"\1", s)
    s = re.sub(r"`+", "", s)
    s = ESCAPE_RE.sub(r"\1", s)
    return " ".join(s.split())


class _Renderer:
    def __init__(self, link, private, id_prefix):
        self.link = link or default_link
        self.private = private
        self.prefix = str(id_prefix or "")
        self.slugs = {}
        self.depth = 0          # lists and quotes inside each other
        self.deadline = time.monotonic() + BUDGET_S
        self.ph = []            # placeholder -> finished HTML
        self.lit = []           # placeholder -> its literal text (for URLs and titles)

    # ---- blocks ----
    def masked(self, raw):
        return bool(self.private and raw and self.private.search(raw))

    def interrupts(self, lines, j):
        """Does line j start a block of its own (and so end the paragraph before it)?"""
        line = lines[j]
        if FENCE_RE.match(line) or HEADING_RE.match(line) or HR_RE.match(line) or QUOTE_RE.match(line):
            return True
        m = LIST_RE.match(line)
        if m and (m.group(4) or "").strip() and (not m.group(2)[0].isdigit() or m.group(2)[:-1] == "1"):
            return True
        return self.is_table(lines, j)

    def blocks(self, lines, tight=False):
        if self.depth > MAX_DEPTH:                          # lists or quotes nested this deep: the rest is plain text
            return self.para([ln for ln in lines if ln.strip()], tight) if any(ln.strip() for ln in lines) else ""
        out, i, n = [], 0, len(lines)
        while i < n:
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            m = FENCE_RE.match(line)
            if m and not (m.group(2)[0] == "`" and "`" in m.group(3)):
                h, i = self.fence(lines, i, m)
                out.append(h)
                continue
            m = HEADING_RE.match(line)
            if m:
                out.append(self.heading(len(m.group(1)), m.group(2) or ""))
                i += 1
                continue
            if HR_RE.match(line):
                out.append("<hr>")
                i += 1
                continue
            if QUOTE_RE.match(line):
                h, i = self.quote(lines, i)
                out.append(h)
                continue
            if self.is_table(lines, i):
                h, i = self.table(lines, i)
                out.append(h)
                continue
            if LIST_RE.match(line):
                h, i = self.list(lines, i)
                out.append(h)
                continue
            j = i + 1
            while j < n and lines[j].strip() and not self.interrupts(lines, j):
                j += 1
            out.append(self.para(lines[i:j], tight))
            i = j
        return "\n".join(out)

    def para(self, lines, tight):
        text = "\n".join(ln.lstrip() for ln in lines).rstrip()
        body = self.inline(text)
        return body if tight else f"<p>{body}</p>"

    def heading(self, level, raw):
        raw = raw.strip()
        hidden = self.masked(raw)
        slug = self.slug("private" if hidden else raw)
        body = PRIVATE if hidden else self.inline(raw)
        return f'<h{level} id="{html.escape(self.prefix + slug)}">{body}</h{level}>'

    def slug(self, raw):
        s = plain_text(raw).lower()
        s = re.sub(r"[^\w\- ]", "", s).strip()
        s = re.sub(r"\s+", "-", s) or "section"
        n = self.slugs.get(s, 0)
        self.slugs[s] = n + 1
        return s if n == 0 else f"{s}-{n}"

    def fence(self, lines, i, m):
        indent, mark, info = len(m.group(1)), m.group(2), m.group(3).strip()
        close = re.compile(r"^ {0,3}" + re.escape(mark[0]) + "{" + str(len(mark)) + r",}[ \t]*$")
        body, j = [], i + 1
        while j < len(lines) and not close.match(lines[j]):
            ln = lines[j]
            k = min(indent, _indent(ln))
            body.append(ln[k:])
            j += 1
        lang = re.sub(r"[^\w+#.-]", "", info.split()[0])[:30] if info else ""
        code = "\n".join(PRIVATE if self.masked(b) else b for b in body)
        cls = f' class="lang-{html.escape(lang)}"' if lang else ""
        return f"<pre><code{cls}>{html.escape(code)}</code></pre>", min(j + 1, len(lines))

    def quote(self, lines, i):
        inner, n = [], len(lines)
        while i < n:
            m = QUOTE_RE.match(lines[i])
            if m:
                inner.append(m.group(1))
            elif lines[i].strip() and inner and inner[-1].strip() and not self.interrupts(lines, i):
                inner.append(lines[i])            # a lazy continuation of the quoted paragraph
            else:
                break
            i += 1
        return f"<blockquote>{self.nested(inner)}</blockquote>", i

    def nested(self, lines, tight=False):
        self.depth += 1
        try:
            return self.blocks(lines, tight)
        finally:
            self.depth -= 1

    def is_table(self, lines, i):
        if i + 1 >= len(lines) or "|" not in lines[i] or not DELIM_ROW_RE.match(lines[i + 1]):
            return False
        return len(split_row(lines[i])) == len(split_row(lines[i + 1]))

    def table(self, lines, i):
        head, aligns = split_row(lines[i]), [_align(c) for c in split_row(lines[i + 1])]
        rows, j = [], i + 2
        while j < len(lines) and lines[j].strip() and "|" in lines[j] and not (
                FENCE_RE.match(lines[j]) or HEADING_RE.match(lines[j]) or QUOTE_RE.match(lines[j])):
            rows.append(split_row(lines[j]))
            j += 1
        ncol = len(head)

        def cells(row, tag):
            row = (row + [""] * ncol)[:ncol]
            return "".join(f"<{tag}{a}>{self.inline(c)}</{tag}>" for c, a in zip(row, aligns, strict=True))   # both ncol long
        out = f'<div class="tablewrap"><table><thead><tr>{cells(head, "th")}</tr></thead>'
        if rows:
            out += "<tbody>" + "".join(f"<tr>{cells(r, 'td')}</tr>" for r in rows) + "</tbody>"
        return out + "</table></div>", j

    def list(self, lines, i):
        n = len(lines)
        first = LIST_RE.match(lines[i])
        ordered = first.group(2)[0].isdigit()
        kind = first.group(2)[-1] if ordered else first.group(2)
        start = int(first.group(2)[:-1]) if ordered else 1

        def same(m):
            mk = m.group(2)
            return (mk[0].isdigit() and ordered and mk[-1] == kind) or (not ordered and mk == kind)
        items, loose = [], False
        while i < n:
            m = LIST_RE.match(lines[i])
            if not m or HR_RE.match(lines[i]) or not same(m):
                break
            ind, mk = len(m.group(1)), m.group(2)
            spaces = len((m.group(3) or " ").expandtabs(4))
            content = m.group(4) or ""
            if spaces > 4:                                  # the content starts further in: keep the extra as text indent
                content, spaces = " " * (spaces - 1) + content, 1
            cindent, nest = ind + len(mk) + spaces, ind + 2
            item = [content]
            i += 1
            while i < n:
                ln = lines[i]
                if not ln.strip():
                    k = i + 1
                    while k < n and not lines[k].strip():
                        k += 1
                    if k < n and _indent(lines[k]) >= nest and not (_indent(lines[k]) < cindent and LIST_RE.match(lines[k])
                                                                    and same(LIST_RE.match(lines[k]))):
                        item.extend([""] * (k - i))
                        loose = True
                        i = k
                        continue
                    break
                li = _indent(ln)
                sub = LIST_RE.match(ln)
                if li >= cindent or (li >= nest and not (sub and same(sub))):
                    item.append(ln[min(li, cindent):])
                elif sub or self.interrupts(lines, i):
                    break
                else:
                    item.append(ln.lstrip())                # a lazy continuation of the item's text
                i += 1
            items.append(item)
            k = i
            while k < n and not lines[k].strip():
                k += 1
            if k > i and k < n and LIST_RE.match(lines[k]) and same(LIST_RE.match(lines[k])) and not HR_RE.match(lines[k]):
                loose = True
                i = k
                continue
            if k > i:
                break
        tag = "ol" if ordered else "ul"
        attrs = f' start="{start}"' if ordered and start != 1 else ""
        lis = []
        for item in items:
            task = TASK_RE.match(item[0]) if item else None
            if task:
                item = [item[0][task.end():]] + item[1:]
            body = self.nested(item, tight=not loose)
            if task:
                done = task.group(1) in "xX"
                lis.append(f'<li class="task"><span class="box">{"&#x2611;" if done else "&#x2610;"}</span> {body}</li>')
            else:
                lis.append(f"<li>{body}</li>")
        return f"<{tag}{attrs}>{''.join(lis)}</{tag}>", i

    # ---- inline ----
    def hold(self, html_, literal=""):
        self.ph.append(html_)
        self.lit.append(literal)
        return f"\x00{len(self.ph) - 1}\x00"

    def literal(self, s):
        """Placeholders back to the text they stand for (a URL or a title must not carry HTML)."""
        return PH_RE.sub(lambda m: self.lit[int(m.group(1))], s)

    def restore(self, s):
        for _ in range(6):                                  # link text may hold code spans: a few rounds at most
            if "\x00" not in s:
                break
            s = PH_RE.sub(lambda m: self.ph[int(m.group(1))], s)
        return s.replace("\x00", "")

    def inline(self, raw):
        if self.masked(raw):
            return PRIVATE
        raw = str(raw).replace("\x00", "")
        if len(raw) > MAX_INLINE or time.monotonic() > self.deadline:
            return html.escape(raw)
        s = CODE_RE.sub(self._code, raw)
        s = ESCAPE_RE.sub(lambda m: self.hold(html.escape(m.group(1)), m.group(1)), s)
        s = BREAK_RE.sub(lambda m: self.hold("<br>") + "\n", s)
        s = BR_TAG_RE.sub(lambda m: self.hold("<br>"), s)
        s = IMAGE_RE.sub(self._image, s)
        s = LINK_RE.sub(self._link, s)
        s = AUTOLINK_RE.sub(lambda m: self._anchor(m.group(1), html.escape(m.group(1)), None), s)
        s = BARE_URL_RE.sub(self._bare, s)
        return self.restore(self.emphasis(html.escape(s)))

    def emphasis(self, s):
        for rx, rep in EMPHASIS:
            s = rx.sub(rep, s)
        return s

    def _code(self, m):
        body = m.group(2).replace("\n", " ")
        if len(body) > 2 and body[0] == " " and body[-1] == " " and body.strip():
            body = body[1:-1]
        return self.hold(f"<code>{html.escape(body)}</code>", body)

    def _image(self, m):
        alt = self.literal(m.group(1)).strip()
        return self.hold(f'<span class="img">[image{": " + html.escape(alt) if alt else ""}]</span>', alt)

    def _link(self, m):
        text = self.restore(self.emphasis(html.escape(m.group(1))))
        url = self.literal(m.group(2)).strip()
        if url.startswith("<") and url.endswith(">"):
            url = url[1:-1].strip()
        title = self.literal(m.group(3)[1:-1]) if m.group(3) else None
        return self._anchor(url, text, title)

    def _bare(self, m):
        url, tail = m.group(0), ""
        while url and url[-1] in ".,:;!?*_~'\"":
            url, tail = url[:-1], url[-1] + tail
        while url.endswith(")") and url.count(")") > url.count("("):
            url, tail = url[:-1], ")" + tail
        return self._anchor(url, html.escape(url), None) + tail

    def _anchor(self, url, text_html, title):
        """One link: the caller's resolver decides what the target becomes; only fixed tags and escaped values come out."""
        url = str(url or "").strip()
        try:
            target = self.link(url) if url else None
        except Exception:  # noqa: BLE001 - a resolver that trips shows the text, never an error page
            target = None
        t = f' title="{html.escape(title)}"' if title else ""
        if target and target[0] == "href" and SAFE_URL_RE.match(str(target[1])):
            return self.hold(f'<a href="{html.escape(str(target[1]))}" rel="noreferrer"{t}>{text_html}</a>')
        if target and target[0] == "anchor":
            frag = re.sub(r"[^\w\-]", "", str(target[1]))
            if frag:
                return self.hold(f'<a href="#{html.escape(self.prefix + frag)}"{t}>{text_html}</a>')
        if target and target[0] == "report" and re.fullmatch(r"[0-9a-f]{6,40}", str(target[1])):
            return self.hold(f'<a href="#" data-report="{target[1]}"{t}>{text_html}</a>')
        return self.hold(f'<span class="xref" title="{html.escape(url)}">{text_html}</span>')


def render(text, link=None, private=None, id_prefix=""):
    """Markdown text -> an HTML fragment (see the module docstring for what is covered and what is escaped)."""
    s = str(text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    if s.startswith("\ufeff"):
        s = s[1:]
    s = FRONT_RE.sub("", s, count=1)
    lines = [_expand(ln) for ln in s.split("\n")]
    return _Renderer(link, private, id_prefix).blocks(lines)

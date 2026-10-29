"""Tests for briandump/mdhtml.py, the Reports tab's Markdown renderer: what it covers, and that nothing in a report can put
markup of its own on the panel (every tag and attribute in the output is one the renderer writes).

    py -m pytest tests/test_mdhtml.py -q
"""
import random
import re
import time
from html.parser import HTMLParser

from briandump import mdhtml
from briandump.mdhtml import render

# ---------- the output allow-list ----------
TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "br", "hr", "strong", "em", "del", "code", "pre", "a", "span", "div", "ul",
        "ol", "li", "blockquote", "table", "thead", "tbody", "tr", "th", "td"}
ATTRS = {"id", "class", "href", "title", "rel", "data-report", "start"}


class Audit(HTMLParser):
    """Every start tag and attribute in a fragment; anything outside the allow-lists is a finding."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.bad = []

    def handle_starttag(self, tag, attrs):
        if tag not in TAGS:
            self.bad.append(f"tag {tag}")
        for k, v in attrs:
            if k not in ATTRS:
                self.bad.append(f"attr {k} on {tag}")
            if k == "href" and not re.match(r"^(https?://|#)", v or "", re.I):
                self.bad.append(f"href {v!r}")

    handle_startendtag = handle_starttag


def audit(fragment):
    a = Audit()
    a.feed(fragment)
    a.close()
    return a.bad


# ---------- blocks ----------
def test_headings_get_ids_and_lose_closing_hashes():
    out = render("# Title\n\n## Second part ##\n\n### C# tips\n\n#### Issue #42\n\n## Second part")
    assert '<h1 id="title">Title</h1>' in out
    assert '<h2 id="second-part">Second part</h2>' in out
    assert '<h3 id="c-tips">C# tips</h3>' in out and '<h4 id="issue-42">Issue #42</h4>' in out
    assert '<h2 id="second-part-1">' in out                                  # a repeated heading gets its own id
    assert render("#hashtag") == "<p>#hashtag</p>"


def test_id_prefix_keeps_ids_and_anchor_links_apart_from_the_page():
    out = render("## Status\n\nSee [the status](#status).", id_prefix="r-")
    assert 'id="r-status"' in out and 'href="#r-status"' in out


def test_paragraphs_soft_and_hard_breaks():
    out = render("one\ntwo  \nthree\\\nfour\n\nnext")
    assert out == "<p>one\ntwo<br>\nthree<br>\nfour</p>\n<p>next</p>"


def test_emphasis_code_and_strike():
    out = render("**bold**, *it*, _it2_, ***both***, ~~gone~~, `x < y`, and __strong__")
    for frag in ("<strong>bold</strong>", "<em>it</em>", "<em>it2</em>", "<strong><em>both</em></strong>", "<del>gone</del>",
                 "<code>x &lt; y</code>", "<strong>strong</strong>"):
        assert frag in out, frag


def test_no_emphasis_inside_words_or_arithmetic():
    out = render("snake_case_name and 2*3*4 and file_name.md")
    assert "<em>" not in out and "snake_case_name" in out and "2*3*4" in out


def test_code_spans_keep_their_text_literally():
    assert render("`**not bold** <b>`") == "<p><code>**not bold** &lt;b&gt;</code></p>"
    assert render("`` a ` b ``") == "<p><code>a ` b</code></p>"


def test_backslash_escapes():
    assert render(r"\*not italic\* and \[not a link\](x)") == "<p>*not italic* and [not a link](x)</p>"


def test_fenced_code_is_escaped_and_tagged_with_its_language():
    out = render("```python\nif a < b and c > d:\n    print('<script>')\n```\n\n~~~\nplain\n~~~")
    assert '<pre><code class="lang-python">if a &lt; b and c &gt; d:\n    print(&#x27;&lt;script&gt;&#x27;)</code></pre>' in out
    assert "<pre><code>plain</code></pre>" in out


def test_an_unclosed_fence_runs_to_the_end():
    assert render("```\nno end\n# not a heading").startswith("<pre><code>no end\n# not a heading</code></pre>")


def test_lists_nested_numbered_and_tasks():
    out = render("- a\n- b\n  - b1\n  - b2\n- c\n\n3. three\n4. four\n\n- [ ] open\n- [x] done")
    assert "<ul><li>a</li><li>b\n<ul><li>b1</li><li>b2</li></ul></li><li>c</li></ul>" in out
    assert '<ol start="3"><li>three</li><li>four</li></ol>' in out
    assert '<li class="task"><span class="box">&#x2610;</span> open</li>' in out
    assert '<li class="task"><span class="box">&#x2611;</span> done</li>' in out


def test_bullets_under_a_numbered_item_with_two_spaces_nest():
    out = render("1. step one\n  - detail\n2. step two")
    assert out == "<ol><li>step one\n<ul><li>detail</li></ul></li><li>step two</li></ol>"


def test_a_loose_list_wraps_items_in_paragraphs():
    out = render("- one\n\n- two\n\n  more about two")
    assert out == "<ul><li><p>one</p></li><li><p>two</p>\n<p>more about two</p></li></ul>"


def test_a_list_item_continues_lazily_and_a_list_can_follow_a_paragraph():
    assert render("- an item that\ncontinues here") == "<ul><li>an item that\ncontinues here</li></ul>"
    assert render("Intro:\n- one\n- two") == "<p>Intro:</p>\n<ul><li>one</li><li>two</li></ul>"
    assert render("The year\n2026. was long") == "<p>The year\n2026. was long</p>"     # only "1." starts a list mid-paragraph


def test_blockquote_holds_blocks():
    out = render("> quoted **text**\n> - a point\n\nafter")
    assert out == "<blockquote><p>quoted <strong>text</strong></p>\n<ul><li>a point</li></ul></blockquote>\n<p>after</p>"


def test_tables_with_alignment_escaped_pipes_and_ragged_rows():
    out = render("| Name | Qty | Note |\n|:---|---:|:---:|\n| a \\| b | 2 | `x\\|y` |\n| short |\n\nafter")
    assert '<div class="tablewrap"><table><thead><tr><th class="al-l">Name</th>' not in out      # left is the default
    assert "<th>Name</th>" in out and '<th class="al-r">Qty</th>' in out and '<th class="al-c">Note</th>' in out
    assert "<td>a | b</td>" in out
    assert "<td class=\"al-c\"><code>x|y</code></td>" in out
    assert '<tr><td>short</td><td class="al-r"></td><td class="al-c"></td></tr>' in out
    assert out.endswith("<p>after</p>")


def test_a_table_can_follow_a_paragraph_line():
    assert "<table>" in render("Results:\n| a | b |\n|---|---|\n| 1 | 2 |")


def test_horizontal_rules_and_front_matter():
    assert render("---\ntitle: x\n---\nbody\n\n***\n\n_ _ _") == "<p>body</p>\n<hr>\n<hr>"


def test_first_heading_skips_code_and_drops_markup():
    assert mdhtml.first_heading("```\n# not this\n```\n## h2\n# The **real** [title](x.md)") == "The real title"
    assert mdhtml.first_heading("no heading") is None


# ---------- links ----------
def test_http_links_keep_their_target_and_others_become_text():
    out = render('[site](https://example.com/a?b=1&c=2 "the site") and [bad](javascript:alert(1)) and [rel](docs/x.md)')
    assert '<a href="https://example.com/a?b=1&amp;c=2" rel="noreferrer" title="the site">site</a>' in out
    assert '<span class="xref" title="javascript:alert(1)">bad</span>' in out
    assert '<span class="xref" title="docs/x.md">rel</span>' in out
    assert "javascript:" not in re.sub(r'title="[^"]*"', "", out)


def test_bare_and_angle_urls_are_linked_without_their_trailing_punctuation():
    out = render("See https://example.com/x). Or <https://example.org/y>, or (https://en.example/a_(b)).")
    assert '<a href="https://example.com/x" rel="noreferrer">https://example.com/x</a>).' in out
    assert '<a href="https://example.org/y" rel="noreferrer">https://example.org/y</a>,' in out
    assert '<a href="https://en.example/a_(b)" rel="noreferrer">' in out


def test_a_resolver_decides_what_a_link_becomes():
    seen = []

    def link(href):
        seen.append(href)
        return ("report", "0123456789ab") if href == "other.md" else None
    out = render("[the other one](other.md) and [gone](missing.md) and [web](https://example.com)", link=link)
    assert '<a href="#" data-report="0123456789ab">the other one</a>' in out
    assert '<span class="xref" title="missing.md">gone</span>' in out
    assert "<a href" not in out.split("gone")[1]                       # the resolver said None for https too
    assert seen == ["other.md", "missing.md", "https://example.com"]


def test_a_resolver_cannot_smuggle_a_bad_target():
    out = render("[x](a.md) [y](b.md) [z](c.md)", link=lambda h: {"a.md": ("href", "javascript:alert(1)"),
                                                                     "b.md": ("report", '"><script>'),
                                                                     "c.md": ("anchor", '"onmouseover=x')}[h])
    assert not audit(out) and "script" not in out.replace("&lt;script", "") and "javascript" not in out.split("title=")[0]
    assert 'href="#onmouseoverx"' in out


def test_images_show_their_alt_text_only():
    assert render("![a chart](chart.png) ![](x.png)") == '<p><span class="img">[image: a chart]</span> <span class="img">[image]</span></p>'


# ---------- escaping ----------
EVIL = [
    "<script>alert(1)</script>",
    '<img src=x onerror="alert(1)">',
    "<a href='javascript:alert(1)'>x</a>",
    '[x](https://example.com" onmouseover="alert(1))',
    "[x](https://example.com 'a\" onmouseover=\"alert(1)')",
    '[x](<javascript:alert(1)>)',
    "[x](JaVaScRiPt:alert(1))",
    "[x](data:text/html;base64,PHNjcmlwdD4=)",
    "[x](vbscript:msgbox)",
    "[x](//evil.example/x)",
    "<https://example.com/\"onmouseover=\"x>",
    "https://example.com/\"><script>alert(1)</script>",
    "| <b>cell</b> | x |\n|---|---|\n| <i>y</i> | <svg onload=alert(1)> |",
    "```\n</code></pre><script>alert(1)</script>\n```",
    "# <script>alert(1)</script>\n\n> <iframe src=x>",
    "- [ ] <img src=x onerror=alert(1)>\n- <style>body{display:none}</style>",
    "&lt;script&gt; &amp; &#60;script&#62;",
    "\x00<script>\x00",
]


def test_raw_html_never_passes_through():
    for src in EVIL:
        out = render(src)
        assert not audit(out), (src, out)
        assert "<script" not in out and "<img" not in out and "<iframe" not in out and "<svg" not in out, (src, out)


def test_entities_in_the_source_are_shown_as_typed():
    assert render("&lt;b&gt; &amp;") == "<p>&amp;lt;b&amp;gt; &amp;amp;</p>"


def test_a_literal_br_is_the_one_tag_taken_from_the_source():
    assert render("| a<br>b | c<BR/>d |\n|---|---|") == ('<div class="tablewrap"><table><thead><tr><th>a<br>b</th><th>c<br>d</th></tr>'
                                                         "</thead></table></div>")
    assert render('<br onclick="x">') == "<p>&lt;br onclick=&quot;x&quot;&gt;</p>"


def test_random_documents_stay_inside_the_allow_list():
    pieces = ["<", ">", "\"", "'", "&", "`", "``", "```", "*", "**", "_", "__", "~~", "[", "]", "(", ")", "](", "!", "#", "##",
              "- ", "1. ", "> ", "|", "---", "\\", "\n", "\n\n", "  \n", " ", "a", "word", "https://ex.example/p?q=1&r=\"2\"",
              "javascript:", "data:", "<script>", "<img src=x onerror=alert(1)>", "<br>", "on", "=", "x.md", "#anchor",
              "\t", ":--", "--:", "[ ] ", "[x] ", "~~~"]
    rng = random.Random(20261019)
    for _ in range(1500):
        src = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 60)))
        out = render(src, link=lambda h: ("report", "abcdef012345") if h.endswith(".md") else mdhtml.default_link(h),
                     private=re.compile("secretword", re.I), id_prefix="r-")
        assert not audit(out), (src, out, audit(out))


# ---------- the private filter ----------
def test_private_blocks_are_masked_and_say_nothing():
    rx = re.compile(r"zephyr", re.I)
    out = render("# Zephyr budget\n\nPublic line.\n\nThe zephyr plan.\n\n- ok\n- zephyr item\n\n| a | b |\n|---|---|\n"
                 "| zephyr | fine |\n\n```\nzephyr = 1\nother = 2\n```\n\n> zephyr quote", private=rx)
    assert "zephyr" not in out.lower()
    assert '<h1 id="private">(private)</h1>' in out
    assert "<p>Public line.</p>" in out and "<p>(private)</p>" in out
    assert "<li>ok</li><li>(private)</li>" in out
    assert "<td>(private)</td><td>fine</td>" in out
    assert "<pre><code>(private)\nother = 2</code></pre>" in out
    assert "<blockquote><p>(private)</p></blockquote>" in out


# ---------- size and speed ----------
def test_pathological_input_renders_quickly():
    nasty = "\n\n".join([
        "**a " * 2000,                                  # openings that never close
        "`" + "``a" * 2000,
        "[" * 3000 + "x" + "]" * 3000 + "(" * 3000,
        "_a " * 3000 + "*b " * 3000,
        "https://example.com/" + "(" * 3000,
        "- " * 400 + "deep",
        "> " * 400 + "deep",
        "| a " * 500 + "|\n" + "|---" * 500 + "|\n" + "| b " * 500 + "|",
        "***a " * 2000, "~~a " * 3000, "[a](" * 3000,
    ])
    t = time.perf_counter()
    out = render(nasty)
    assert time.perf_counter() - t < mdhtml.BUDGET_S                       # linear patterns: well inside the budget
    assert not audit(out)


def test_a_huge_paragraph_is_escaped_without_inline_formatting():
    big = "**x** " * (mdhtml.MAX_INLINE // 6 + 10) + "<b>"
    out = render(big)
    assert "<strong>" not in out and out.endswith("&lt;b&gt;</p>")


def test_after_the_time_budget_the_rest_is_plain_escaped_text(monkeypatch):
    monkeypatch.setattr(mdhtml, "BUDGET_S", -1.0)
    out = render("# **Title**\n\n- *item* <i>\n\n| `a` |\n|---|")
    assert "<h1" in out and "<li>" in out and "<table>" in out              # the structure stays
    assert "<strong>" not in out and "<em>" not in out and "<code>" not in out and "&lt;i&gt;" in out

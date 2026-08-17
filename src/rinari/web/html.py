"""HTML content extraction for web.* tools (phase 5).

Stdlib-only parsing: enough structure (title, meta, links, lightweight text,
markdown, tables) for the web tools without a parser dependency. Parsed
content is untrusted remote data — it is only rendered into bounded data
structures, never executed (harness.md: untrusted web content stays
untrusted).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin

MAX_LINKS = 500
MAX_TABLES = 10
MAX_TABLE_ROWS = 50
MAX_TABLE_CELLS = 50

_SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})
_HEADING_TAGS = {h: "#" * (i + 1) for i, h in enumerate(("h1", "h2", "h3", "h4", "h5", "h6"))}
_META_NAMES = frozenset(
    {
        "description",
        "keywords",
        "author",
        "generator",
        "og:title",
        "og:description",
        "og:image",
        "og:url",
        "twitter:title",
        "twitter:description",
    }
)


@dataclass(slots=True)
class Page:
    title: str = ""
    meta: dict[str, str] = field(default_factory=dict)
    language: str = ""
    charset: str = ""
    links: list[dict] = field(default_factory=list)
    tokens: list[tuple] = field(default_factory=list)
    tables: list[dict] = field(default_factory=list)

    @property
    def description(self) -> str:
        return self.meta.get("description") or self.meta.get("og:description") or ""

    def text(self) -> str:
        parts: list[str] = []
        for token in self.tokens:
            kind = token[0]
            if kind in ("t", "code"):
                parts.append(token[1])
            elif kind in ("h", "a"):
                parts.append(token[2])
            elif kind == "li":
                parts.append(token[1])
        return "\n".join(p for p in parts if p and p.strip())

    def markdown(self) -> str:
        parts: list[str] = []
        for token in self.tokens:
            kind = token[0]
            if kind == "t":
                parts.append(token[1])
            elif kind == "h":
                parts.append(f"{token[1]} {token[2]}")
            elif kind == "a":
                href, text = token[1], token[2].strip()
                parts.append(f"[{text or href}]({href})")
            elif kind == "li":
                parts.append(f"- {token[1]}")
            elif kind == "code":
                code = token[1].strip("\n")
                parts.append(f"```\n{code}\n```" if "\n" in code else f"`{code}`")
        return "\n\n".join(p for p in parts if p and p.strip())


class _PageParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self.page = Page()
        self._skip_depth = 0
        self._in_title = False
        self._title: list[str] = []
        self._link_stack: list[dict] = []
        self._heading: dict | None = None
        self._list_depth = 0
        self._in_code = False
        self._table_depth = 0
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    # -- start tags ----------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attr = dict(attrs)
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "html" and attr.get("lang"):
            self.page.language = attr["lang"]
            return
        if tag == "title" and self._in_title is False and not self.page.title:
            self._in_title = True
            return
        if tag == "meta":
            self._handle_meta(attr)
            return
        if tag == "a":
            self._link_stack.append({"href": attr.get("href") or "", "text": []})
            return
        if tag in _HEADING_TAGS:
            self._heading = {"level": _HEADING_TAGS[tag], "text": []}
            return
        if tag in ("ul", "ol"):
            self._list_depth += 1
            return
        if tag == "li":
            self._list_item("")
            return
        if tag == "table":
            self._table_depth += 1
            if self._table is None and len(self.page.tables) < MAX_TABLES:
                self._table = []
            return
        if self._table is not None and tag == "tr":
            self._row = [] if len(self._table) < MAX_TABLE_ROWS else None
            if self._row is not None:
                self._table.append(self._row)
            return
        if self._table is not None and tag in ("td", "th"):
            self._cell = [] if self._row is not None else "discard"
            return
        if tag == "code" and not self._in_code:
            self._in_code = True
            self.page.tokens.append(("code", ""))

    def _handle_meta(self, attr: dict) -> None:
        charset = attr.get("charset")
        content = attr.get("content") or ""
        if charset:
            self.page.charset = charset
        elif "charset=" in content.lower():
            self.page.charset = content.lower().partition("charset=")[2].split(";")[0].strip()
        key = (attr.get("name") or attr.get("property") or "").lower()
        if key in _META_NAMES and content:
            self.page.meta.setdefault(key, content)

    # -- end tags --------------------------------------------------------------

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title" and self._in_title:
            self.page.title = " ".join("".join(self._title).split())
            self._in_title = False
            self._title = []
            return
        if tag in _HEADING_TAGS and self._heading is not None:
            text = " ".join("".join(self._heading["text"]).split())
            if text:
                self.page.tokens.append(("h", self._heading["level"], text))
            self._heading = None
            return
        if tag in ("ul", "ol"):
            self._list_depth = max(0, self._list_depth - 1)
            return
        if tag == "a" and self._link_stack:
            self._record_link(self._link_stack.pop())
            return
        if tag in ("td", "th") and self._cell is not None:
            if (
                isinstance(self._cell, list)
                and self._row is not None
                and len(self._row) < MAX_TABLE_CELLS
            ):
                self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
            return
        if tag == "tr" and self._row is not None:
            self._row = None
            return
        if tag == "table":
            self._table_depth = max(0, self._table_depth - 1)
            if self._table is not None:
                self.page.tables.append({"rows": self._table})
                self._table = None
                self._row = None
                self._cell = None
            return
        if tag == "code":
            self._in_code = False

    # -- data ------------------------------------------------------------------

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title.append(data)
            return
        if self._skip_depth or not data or not data.strip():
            return
        if self._cell == "discard":
            return
        if isinstance(self._cell, list):
            self._cell.append(data)
            return
        if self._heading is not None:
            self._heading["text"].append(data)
            return
        if self._link_stack:
            self._link_stack[-1]["text"].append(data)
            return
        if self._in_code:
            tokens = self.page.tokens
            if tokens and tokens[-1][0] == "code":
                tokens[-1] = ("code", tokens[-1][1] + data)
            else:
                tokens.append(("code", data))
            return
        if self._list_depth:
            self._list_item(data)
            return
        self.page.tokens.append(("t", data))

    def _list_item(self, data: str) -> None:
        tokens = self.page.tokens
        if tokens and tokens[-1][0] == "li":
            tokens[-1] = ("li", tokens[-1][1] + data)
        else:
            tokens.append(("li", data))

    def _record_link(self, link: dict) -> None:
        href = link["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "#", "data:")):
            return
        absolute = urldefrag(urljoin(self._base_url, href)).url
        if not absolute:
            return
        text = " ".join("".join(link["text"]).split())
        self.page.tokens.append(("a", absolute, text))
        if any(item["href"] == absolute for item in self.page.links):
            return
        if len(self.page.links) < MAX_LINKS:
            self.page.links.append({"href": absolute, "text": text})


def parse_page(html: str, base_url: str = "") -> Page:
    parser = _PageParser(base_url)
    parser.feed(html)
    parser.close()
    return parser.page


__all__ = ["MAX_LINKS", "Page", "parse_page"]

"""Extract plain text for content checks; the result is not sanitized HTML."""

from __future__ import annotations

from html.parser import HTMLParser

_IGNORED_ELEMENTS = frozenset({"head", "script", "style", "template", "title"})
_HEAD_ELEMENTS = frozenset(
    {"base", "link", "meta", "noscript", "script", "style", "template", "title"}
)
_SEPARATORS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "caption",
        "center",
        "dd",
        "details",
        "dialog",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "legend",
        "li",
        "main",
        "menu",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # HTML allows </head> to be omitted before body content.
        if self.ignored == ["head"] and tag not in _HEAD_ELEMENTS:
            self.ignored.clear()
        if tag in _IGNORED_ELEMENTS:
            self.ignored.append(tag)
        elif not self.ignored and tag in _SEPARATORS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if self.ignored:
            if tag in self.ignored:
                index = len(self.ignored) - 1 - self.ignored[::-1].index(tag)
                del self.ignored[index:]
        elif tag in _SEPARATORS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self.ignored == ["head"] and data.strip():
            self.ignored.clear()
        if not self.ignored:
            self.parts.append(data)


def html_to_text(value: str) -> str:
    """Strip markup and script/style content, decode entities and collapse whitespace.

    Block boundaries separate words; inline formatting preserves word fragments.
    This extracts text without running scripts, fetching resources or applying CSS.
    Encoded literal angle brackets remain text and must be escaped when rendered.
    """
    parser = _TextParser()
    parser.feed(value)
    parser.close()
    text = "".join(parser.parts).replace("\u200b", "").replace("\ufeff", "")
    return " ".join(text.split())

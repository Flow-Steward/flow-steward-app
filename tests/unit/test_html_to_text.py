"""HTML text extraction through the public transform action."""

from __future__ import annotations

import pytest

from core.application.orchestration.transform_step_runtime import execute_transform_step


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("", ""),
        (" \t\n ", ""),
        ("<p>&nbsp; &#160; </p>", ""),
        ('<p class="MsoNormal"><span style="font:12pt Calibri">Два слова</span></p>', "Два слова"),
        ("<p>Первый</p><p>Второй</p>", "Первый Второй"),
        ("One<br>two<br/>three<hr>four", "One two three four"),
        ("<table><tr><td>A</td><td>B</td></tr></table>", "A B"),
        ("pre<b>fix</b>, <i>hello</i>!", "prefix, hello!"),
        ("<p>A &amp; B &#x1F642;</p>", "A & B 🙂"),
        ("&lt;b&gt;literal&lt;/b&gt;", "<b>literal</b>"),
        ("<p>Text<!-- Word metadata --> stays</p>", "Text stays"),
        ("<SCRIPT>ignored()</SCRIPT><style>.x{color:red}</style><p>Keep</p>", "Keep"),
        ("<head><title>Metadata</title></head><body>Body</body>", "Body"),
        ("<head><title>Metadata</title><body>Body</body>", "Body"),
        ("<head><title>Metadata</title><p>Description</p>", "Description"),
        ("<head><title>Metadata</title>Body text", "Body text"),
        ("<details><summary>Size</summary>Large</details>", "Size Large"),
        ("<center>First</center>Second", "First Second"),
        ("<template><template>Hidden</template>too</template>Shown", "Shown"),
        ("<p>Keep</p><script>unfinished", "Keep"),
        ("<p>Unclosed <strong>bold", "Unclosed bold"),
        ('<p onclick="ignored()">Text</p><img src="unused" alt="Not body text">', "Text"),
        ("<p>\u200b\ufeff</p>", ""),
        ("👩\u200d💻", "👩\u200d💻"),
    ],
)
def test_html_to_text_extracts_content_without_counting_markup(source: str, expected: str) -> None:
    result = execute_transform_step(
        config={
            "input": {"source": "state.product"},
            "mode": "object",
            "result_target": "state.cleaned",
            "fields": [
                {
                    "from": "description_html",
                    "to": "description",
                    "type": "text",
                    "actions": [{"action": "text.html_to_text"}],
                },
                {
                    "from": "description_html",
                    "to": "length",
                    "type": "number",
                    "actions": [{"action": "text.html_to_text"}, {"action": "text.length"}],
                },
            ],
        },
        state={"product": {"description_html": source}},
    )

    assert result["ok"] is True, result
    assert result["output"] == {"description": expected, "length": len(expected)}

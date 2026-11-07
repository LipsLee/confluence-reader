from __future__ import annotations

from confluence_reader.markdown import (
    body_to_storage,
    markdown_to_storage,
    storage_to_markdown,
)


def test_markdown_to_storage_common_blocks() -> None:
    storage = markdown_to_storage(
        "# Title\n\n- one\n- two\n\n**bold** and `code`\n\n"
        "| A | B |\n|---|---|\n| 1 | 2 |\n"
    )
    assert "<h1>Title</h1>" in storage
    assert "<ul>" in storage
    assert "<strong>bold</strong>" in storage
    assert "<code>code</code>" in storage
    assert "<table>" in storage


def test_markdown_escapes_raw_html() -> None:
    storage = markdown_to_storage("<script>alert(1)</script>")
    assert "<script>" not in storage
    assert "&lt;script&gt;" in storage


def test_attachment_image_is_converted_to_confluence_macro() -> None:
    storage = markdown_to_storage("![diagram](attachment://drawing.pdf)")
    assert "<ac:image" in storage
    assert 'ri:filename="drawing.pdf"' in storage


def test_storage_to_markdown_preserves_code_and_table() -> None:
    markdown = storage_to_markdown(
        "<h1>Title</h1><p>Text</p><pre>code</pre>"
        "<table><tr><th>A</th></tr><tr><td>1</td></tr></table>"
    )
    assert "# Title" in markdown
    assert "```" in markdown
    assert "| A |" in markdown


def test_storage_body_passthrough() -> None:
    value = '<ac:structured-macro ac:name="info"><ac:rich-text-body>Hi</ac:rich-text-body></ac:structured-macro>'
    assert body_to_storage(value, "storage") == value

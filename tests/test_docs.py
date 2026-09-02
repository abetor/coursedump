"""A saved web page (.mht/.mhtml) -> text.

Courses carry extra materials as pages saved by a browser. That is not html but
a MIME container: markitdown read it as flat text and put raw quoted-printable
mixed with base64 images into the snapshot (53 files in one course).
"""

import quopri
import shutil
from base64 import b64encode

import pytest
import pymupdf
import pymupdf4llm

from coursedump.executor import Opts, run_course
from coursedump.extractors import docs
from coursedump.manifest import kind_of

# Russian text in windows-1251: a Cyrillic code page is the only way to tell a
# correctly decoded charset from a wrong one.
ARTICLE = (
    "<html><head><title>Мьютексы</title>"
    '<meta http-equiv="Content-Type" content="text/html; charset=windows-1251">'
    "</head><body><h1>Танцы с мьютексами</h1>"
    "<p>Мьютекс защищает общее состояние от гонки.</p></body></html>"
)


class _PdfPage:
    def __init__(self, text):
        self.text = text

    def get_text(self):
        return self.text


class _PdfDocument:
    def __init__(self, texts):
        self.pages = [_PdfPage(text) for text in texts]
        self.page_count = len(self.pages)

    def __getitem__(self, number):
        return self.pages[number]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _saved_page(path, charset="windows-1251", *, mime_charset=True):
    """A Chrome-style saved page: the html part plus an image."""
    html = quopri.encodestring(ARTICLE.encode(charset)).decode("ascii")
    png = b64encode(b"\x89PNG" + b"\x00" * 64).decode("ascii")
    html_content_type = "Content-Type: text/html"
    if mime_charset:
        html_content_type += f'; charset="{charset}"'
    path.write_bytes(
        "\r\n".join(
            [
                "From: <Saved by Blink>",
                "Snapshot-Content-Location: https://example.org/lesson",
                "Subject: =?utf-8?Q?=D0=A3=D1=80=D0=BE=D0=BA?=",
                "MIME-Version: 1.0",
                'Content-Type: multipart/related; type="text/html";'
                ' boundary="----MultipartBoundary"',
                "",
                "------MultipartBoundary",
                html_content_type,
                "Content-Transfer-Encoding: quoted-printable",
                "Content-Location: https://example.org/lesson",
                "",
                html,
                "------MultipartBoundary",
                "Content-Type: image/png",
                "Content-Transfer-Encoding: base64",
                "Content-Location: https://example.org/pic.png",
                "",
                png,
                "------MultipartBoundary--",
                "",
            ]
        ).encode(charset)
    )
    return path


def _saved_related(path, parts, *, start="<root>"):
    """multipart/related with a controlled order of html parts and Content-IDs."""
    lines = [
        "MIME-Version: 1.0",
        f'Content-Type: multipart/related; start="{start}"; boundary="BOUNDARY"',
        "",
    ]
    for content_id, body in parts:
        encoded = quopri.encodestring(body.encode("utf-8")).decode("ascii")
        lines.extend([
            "--BOUNDARY",
            'Content-Type: text/html; charset="utf-8"',
            f"Content-ID: <{content_id}>",
            "Content-Transfer-Encoding: quoted-printable",
            "",
            encoded,
        ])
    lines.extend(["--BOUNDARY--", ""])
    path.write_bytes("\r\n".join(lines).encode("ascii"))
    return path


def test_pdf_falls_back_per_page_without_losing_text(tmp_path, monkeypatch):
    path = tmp_path / "tables.pdf"
    calls = []

    def to_markdown(_path, *, pages=None, show_progress=False):
        calls.append(pages)
        if pages is None:
            raise AttributeError("grid.h_lines")
        if pages == [1]:
            raise AttributeError("grid=None")
        return f"markdown of page {pages[0]}"

    monkeypatch.setattr(pymupdf4llm, "to_markdown", to_markdown)
    monkeypatch.setattr(docs, "_without_tables_kwargs", lambda _module: None)
    monkeypatch.setattr(
        pymupdf,
        "open",
        lambda _path: _PdfDocument(["plain 0", "plain page 1", "plain 2"]),
    )

    text = docs.pdf_markdown(path)

    assert calls == [None, [0], [1], [2]]
    assert "# EXTRACTION:" in text
    assert "1 of 3 pages" in text and "zero-based: 1" in text
    expected = ["markdown of page 0", "plain page 1", "markdown of page 2"]
    assert all(value in text for value in expected)
    assert [text.index(value) for value in expected] == sorted(
        text.index(value) for value in expected
    )


def test_pdf_retries_without_tables_when_backend_supports_it(tmp_path, monkeypatch):
    path = tmp_path / "tables.pdf"
    strategies = []

    def to_markdown(
        _path, *, pages=None, table_strategy="lines_strict", show_progress=False
    ):
        strategies.append(table_strategy)
        if table_strategy:
            raise AttributeError("grid.h_lines")
        return "text without tables"

    monkeypatch.setattr(pymupdf4llm, "_use_layout", False)
    monkeypatch.setattr(pymupdf4llm.helpers.pymupdf_rag, "to_markdown", to_markdown)

    text = docs.pdf_markdown(path)

    assert strategies == ["lines_strict", None]
    assert "without table recognition" in text
    assert text.endswith("text without tables")


def test_healthy_pdf_does_not_enable_fallback(tmp_path, monkeypatch):
    calls = []

    def to_markdown(_path, *, pages=None, show_progress=False):
        calls.append(pages)
        return "healthy markdown"

    monkeypatch.setattr(pymupdf4llm, "to_markdown", to_markdown)

    text = docs.pdf_markdown(tmp_path / "healthy.pdf")

    assert text == "healthy markdown"
    assert calls == [None]
    assert "# EXTRACTION:" not in text


@pytest.mark.parametrize("ext", [".mht", ".mhtml"])
def test_saved_page_is_extractable_kind(ext):
    assert kind_of(f"Доп.материалы/статья{ext}") == "html"


def test_mhtml_html_decodes_declared_charset(tmp_path):
    html = docs.mhtml_html(_saved_page(tmp_path / "article.mht"))

    assert "Танцы с мьютексами" in html
    assert "�" not in html


def test_mhtml_html_uses_meta_charset_when_mime_omits_it(tmp_path):
    html = docs.mhtml_html(
        _saved_page(tmp_path / "no-mime-charset.mhtml", mime_charset=False)
    )

    assert "Танцы с мьютексами" in html
    assert "�" not in html


def test_mhtml_start_selects_root_after_an_earlier_frame(tmp_path):
    path = _saved_related(
        tmp_path / "with-frame.mhtml",
        [("frame", "<html><body>FRAME TEXT</body></html>"),
         ("root", "<html><body>ROOT TEXT</body></html>")],
    )

    html = docs.mhtml_html(path)

    assert "ROOT TEXT" in html
    assert "FRAME TEXT" not in html


def test_mhtml_empty_started_root_is_not_replaced_with_a_frame(tmp_path):
    path = _saved_related(
        tmp_path / "empty-root.mhtml",
        [("frame", "<html><body>FRAME TEXT</body></html>"), ("root", "")],
    )

    with pytest.raises(ValueError, match="has no text part"):
        docs.mhtml_html(path)


def test_mhtml_markdown_drops_container_and_keeps_text(tmp_path):
    text = docs.html_markdown(_saved_page(tmp_path / "article.mht"))

    assert "Мьютекс защищает общее состояние от гонки." in text
    # neither container markup nor the base64 image may reach the snapshot
    assert "quoted-printable" not in text
    assert "MultipartBoundary" not in text
    assert "iVBORw" not in text and "PNG" not in text


def test_mhtml_without_text_part_is_an_error(tmp_path):
    path = tmp_path / "broken.mht"
    path.write_bytes(b"From: <Saved by Blink>\r\nContent-Type: image/png\r\n\r\nx\r\n")

    with pytest.raises(ValueError, match="has no text part"):
        docs.mhtml_html(path)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_saved_page_reaches_the_dump(tmp_path):
    course = tmp_path / "Курс"
    (course / "Урок 01" / "Доп.материалы").mkdir(parents=True)
    _saved_page(course / "Урок 01" / "Доп.материалы" / "[habr.com] статья.mht")

    stats = run_course(str(course), Opts(out_root=tmp_path / "out", asr_backend="dummy"))

    assert (stats["done"], stats["total"], stats["errors"]) == (1, 1, 0)
    dumps = list((tmp_path / "out").rglob("*.mht.md"))
    assert len(dumps) == 1
    body = dumps[0].read_text(encoding="utf-8")
    assert "# статья" in body  # title without the extension
    assert "Мьютекс защищает общее состояние от гонки." in body

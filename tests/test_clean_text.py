"""Snapshot bodies carry no control or format characters.

Incident 2026-08-28: a BOM (U+FEFF) at the head of a .txt file and 161 DEL
(0x7f) characters out of an epub went into text/*.md untouched. The consumer
downstream rejects any character in Unicode category C except \\n/\\t, plus Zl
and Zp, so it refused the documents and three of its workers stopped hard.
"""

import unicodedata

from coursedump.executor import Opts, run_course
from coursedump.util import clean_text


def _forbidden(text: str) -> list[str]:
    """The same predicate the downstream consumer applies to a document."""
    return [hex(ord(ch)) for ch in text
            if ch not in "\n\t"
            and (unicodedata.category(ch).startswith("C")
                 or unicodedata.category(ch) in {"Zl", "Zp"})]


def test_clean_text_drops_control_and_format_keeps_spaces_and_emoji():
    raw = ("﻿https://x\x7f\r\n"          # BOM, DEL, CRLF
           "tab\tand nbsp ​­ "    # tab, NBSP, ZWSP, soft hyphen
           "кон́ец next "             # combining acute, Zl
           "\U0001F468‍\U0001F4BB "        # emoji joined by a ZWJ
           "\x00\x1b[0m\udcff")                 # NUL, ESC, lone surrogate
    out = clean_text(raw)
    assert _forbidden(out) == []
    assert out.startswith("https://x\n")
    assert "tab\tand nbsp  " in out           # tab and NBSP are not junk
    assert "кон́ец\nnext" in out             # acute intact, Zl becomes a newline
    assert "\U0001F468\U0001F4BB" in out           # ZWJ dropped, emoji kept
    assert out.endswith("next \U0001F468\U0001F4BB [0m")


def test_clean_text_is_identity_for_clean_text():
    text = "# Заголовок\n\nобычный текст, кириллица и\tтаб\n"
    assert clean_text(text) == text


def test_pipeline_output_md_has_no_forbidden_chars(tmp_path):
    root = tmp_path / "in" / "Курс"
    root.mkdir(parents=True)
    (root / "Дополнительные материалы.txt").write_bytes(
        "﻿https://example.org/doc\n\nlist below \x7f\n".encode("utf-8"))
    stats = run_course(str(root), Opts(out_root=tmp_path / "out", asr_backend="dummy"))
    assert stats["done"] == stats["total"] == 1
    md = tmp_path / "out" / "Курс" / "text" / "Дополнительные материалы.txt.md"
    body = md.read_text(encoding="utf-8")
    assert _forbidden(body) == []
    assert "\nhttps://example.org/doc\n\nlist below\n" in body

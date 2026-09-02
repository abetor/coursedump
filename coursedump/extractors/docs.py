"""Convert documents to Markdown."""

import inspect
import re
import tempfile
from email import policy
from email.parser import BytesParser
from pathlib import Path

MHTML = {".mht", ".mhtml"}
_META_CHARSET = re.compile(
    br'<meta\b[^>]*\bcharset\s*=\s*["\']?\s*([a-z0-9._:-]+)', re.IGNORECASE
)


def _without_tables_kwargs(pymupdf4llm) -> dict[str, object] | None:
    """Return a real active-backend option, never a flag swallowed by **kwargs."""
    if getattr(pymupdf4llm, "_use_layout", False):
        implementation = getattr(
            pymupdf4llm, "_layout_to_markdown", pymupdf4llm.to_markdown
        )
    else:
        implementation = getattr(
            getattr(getattr(pymupdf4llm, "helpers", None), "pymupdf_rag", None),
            "to_markdown",
            pymupdf4llm.to_markdown,
        )
    try:
        parameters = inspect.signature(implementation).parameters
    except (TypeError, ValueError):
        return None
    if "ignore_tables" in parameters:
        return {"ignore_tables": True}
    if "table_strategy" in parameters:
        # In the legacy pymupdf4llm backend, a falsy strategy skips find_tables.
        return {"table_strategy": None}
    return None


def _failure_text(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def pdf_markdown(path: Path) -> str:
    import pymupdf
    import pymupdf4llm

    source = str(path)
    try:
        return pymupdf4llm.to_markdown(source, show_progress=False)
    except Exception as whole_error:
        without_tables = _without_tables_kwargs(pymupdf4llm)
        if without_tables is not None:
            try:
                text = pymupdf4llm.to_markdown(
                    source, show_progress=False, **without_tables
                )
            except Exception:
                pass
            else:
                return (
                    "# EXTRACTION: whole-document pymupdf4llm failed "
                    f"({_failure_text(whole_error)}); the document was extracted again "
                    "without table recognition\n\n"
                    + text
                )

        parts = []
        page_errors = []
        with pymupdf.open(source) as document:
            page_count = document.page_count
            for number in range(page_count):
                try:
                    text = pymupdf4llm.to_markdown(
                        source, pages=[number], show_progress=False
                    )
                except Exception as exc:
                    page_errors.append((number, exc))
                    text = document[number].get_text()
                parts.append(text)

        if page_errors:
            numbers = ", ".join(str(number) for number, _ in page_errors)
            details = "; ".join(
                f"{number}: {_failure_text(exc)}" for number, exc in page_errors
            )
            note = (
                "# EXTRACTION: whole-document pymupdf4llm failed "
                f"({_failure_text(whole_error)}) and failed on {len(page_errors)} of "
                f"{page_count} pages (zero-based: {numbers}; {details}); text from "
                "those pages was extracted without layout"
            )
        else:
            note = (
                "# EXTRACTION: whole-document pymupdf4llm failed "
                f"({_failure_text(whole_error)}); the document was extracted page by page, "
                "pages without layout: 0"
            )
        return note + "\n\n" + "\n\n".join(parts)


def _markitdown(path: Path) -> str:
    from markitdown import MarkItDown

    return MarkItDown(enable_plugins=False).convert(str(path)).text_content or ""


def office_markdown(path: Path) -> str:
    return _markitdown(path)


def _html_charset(raw: bytes) -> str | None:
    """Read an ASCII-compatible meta charset before the first HTML decode."""
    match = _META_CHARSET.search(raw)
    return match.group(1).decode("ascii") if match else None


def _decode_html(raw: bytes, mime_charset: str | None) -> str:
    """Prefer MIME charset, then meta, then a safe UTF-8 fallback."""
    for charset in (mime_charset, _html_charset(raw), "utf-8"):
        if not charset:
            continue
        try:
            return raw.decode(charset, errors="replace")
        except LookupError:
            continue
    return raw.decode("utf-8", errors="replace")


def _content_id(value: object) -> str:
    return str(value or "").strip().strip("<>").strip()


def mhtml_html(path: Path) -> str:
    """Extract the root page from a saved MIME container (.mht/.mhtml).

    A browser-saved page is multipart/related: HTML plus images, CSS, and fonts.
    MarkItDown reads the container as flat text and returns raw MIME, so the
    HTML part is selected here. The multipart `start` parameter identifies the
    root Content-ID; otherwise the first HTML part is the root and later parts
    are frames.
    """
    with path.open("rb") as fh:
        message = BytesParser(policy=policy.default).parse(fh)
    parts = list(message.walk())
    start = _content_id(message.get_param("start"))
    if start:
        part = next(
            (candidate for candidate in parts
             if _content_id(candidate.get("Content-ID")) == start),
            None,
        )
        if part is None or part.get_content_type() not in {"text/html", "text/plain"}:
            raise ValueError(f"MHTML has no text part: {path.name}")
    else:
        part = next(
            (candidate for candidate in parts
             if candidate.get_content_type() == "text/html"),
            None,
        )
        if part is None:
            part = next(
                (candidate for candidate in parts
                 if candidate.get_content_type() == "text/plain"),
                None,
            )
    if part is None:
        raise ValueError(f"MHTML has no text part: {path.name}")
    raw = part.get_payload(decode=True)
    if not raw:
        raise ValueError(f"MHTML has no text part: {path.name}")
    return _decode_html(raw, part.get_content_charset())


def html_markdown(path: Path) -> str:
    if path.suffix.lower() not in MHTML:
        return _markitdown(path)  # MarkItDown already handles plain HTML.
    html = mhtml_html(path)
    with tempfile.TemporaryDirectory() as tmp:
        # The part is already decoded to str but may retain a foreign <meta
        # charset>. Write UTF-8 and declare it so MarkItDown does not decode the
        # temporary file using stale metadata.
        page = Path(tmp) / "page.html"
        page.write_text('<meta charset="utf-8">\n' + html, encoding="utf-8")
        return _markitdown(page)

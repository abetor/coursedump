"""Fetch text-only Boosty posts through the API already used by yt-dlp."""

from __future__ import annotations

import json
import random
import time
import urllib.parse
from dataclasses import dataclass


MEDIA_TYPES = {"ok_video", "video", "audio_file", "ok_audio"}
_sleep = time.sleep
_uniform = random.uniform


class TextPostError(RuntimeError):
    pass


@dataclass(frozen=True)
class TextPost:
    title: str
    body: str


def _text(block: dict) -> str:
    raw = block.get("content") or ""
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return str(raw)
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], str):
        return parsed[0]
    return ""


def _public_url(value: object) -> str:
    """Return a URL without query or fragment so temporary signatures never enter the corpus."""
    raw = str(value or "")
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return raw.partition("?")[0].partition("#")[0]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _render_list_items(items: object, depth: int = 0) -> list[str]:
    out: list[str] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        inner = [line for line in render_blocks(item.get("data") or []) if line.strip()]
        if inner:
            out.append("  " * depth + "- " + " ".join(inner))
        out.extend(_render_list_items(item.get("items") or [], depth + 1))
    return out


def _walk_blocks(value: object):
    """Yield every typed block, including data/items in nested lists."""
    if isinstance(value, list):
        for item in value:
            yield from _walk_blocks(item)
    elif isinstance(value, dict):
        if value.get("type"):
            yield value
        for key in ("data", "items"):
            yield from _walk_blocks(value.get(key))


def render_blocks(data: list) -> list[str]:
    """Render deterministic plain text for text, header, list, link, and file blocks."""
    out: list[str] = []
    for block in data:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            out.append("" if block.get("modificator") == "BLOCK_END" else _text(block))
        elif kind == "header":
            out.extend(("", "## " + _text(block), ""))
        elif kind == "link":
            raw_url = str(block.get("url") or "")
            url = _public_url(raw_url)
            label = _text(block)
            out.append(url if label in ("", raw_url, url) else f"{label} ({url})")
        elif kind == "list":
            out.extend(_render_list_items(block.get("items") or []))
        elif kind == "file":
            size_mb = round((block.get("size") or 0) / 1048576, 1)
            out.append(
                f"[file: {block.get('title') or '?'} "
                f"({_public_url(block.get('url'))}, {size_mb} MB)]"
            )
    return out


def squeeze(lines: list[str]) -> str:
    body: list[str] = []
    blank = 0
    for line in lines:
        value = line.rstrip()
        if not value:
            blank += 1
            if blank > 1 or not body:
                continue
        else:
            blank = 0
        body.append(value)
    return "\n".join(body).strip("\n")


def pause_after_no_videos() -> None:
    """Separate a failed resolver call from the fallback API by 3-8 seconds."""
    _sleep(_uniform(3, 8))


def from_api(post: dict) -> TextPost:
    if not post.get("hasAccess"):
        raise TextPostError("hasAccess=false - this account cannot access the post text")
    data = post.get("data") or []
    media = [block.get("type") for block in _walk_blocks(data)
             if block.get("type") in MEDIA_TYPES]
    if media:
        raise TextPostError(f"API post contains media {media}; it is not a text-only fallback")
    body = squeeze(render_blocks(data))
    if not body:
        raise TextPostError("API post does not contain supported text")
    return TextPost(title=(post.get("title") or "").strip(), body=body)


def ydl_params(cookies_from_browser: str) -> dict[str, object]:
    params: dict[str, object] = {"quiet": True, "no_warnings": True, "skip_download": True}
    if cookies_from_browser:
        params["cookiesfrombrowser"] = (cookies_from_browser,)
    return params


def fetch(url: str, cookies_from_browser: str = "") -> TextPost:
    """Fetch post JSON through BoostyIE without creating cookie or token files."""
    from yt_dlp import YoutubeDL
    from yt_dlp.extractor.boosty import BoostyIE

    with YoutubeDL(ydl_params(cookies_from_browser)) as ydl:
        extractor = BoostyIE(ydl)
        match = extractor._match_valid_url(url)
        if match is None:
            raise TextPostError(f"not a Boosty post URL: {url}")
        user, post_id = match.group("user", "post_id")
        headers = {}
        auth = extractor._get_cookies("https://boosty.to/").get("auth")
        if auth is not None:
            try:
                value = json.loads(urllib.parse.unquote(auth.value))
                headers["Authorization"] = f"Bearer {value['accessToken']}"
            except (json.JSONDecodeError, KeyError):
                pass
        post = extractor._download_json(
            f"https://api.boosty.to/v1/blog/{user}/post/{post_id}",
            post_id,
            note="Downloading text post data",
            errnote="Unable to download text post data",
            headers=headers,
        )
    return from_api(post)


def is_no_videos(error: BaseException) -> bool:
    return "no videos found" in str(error).casefold()

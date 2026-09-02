"""Boosty text posts, no live network.

The captured fixture and the lines expected from it stay in Russian: it is a
real post shape from a Russian-language platform, and the assertions follow the
text from JSON escaping through the renderer into the corpus file.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from coursedump import boosty_text, executor, sources
from coursedump.cli import app


POST = "https://boosty.to/author/posts/11111111-2222-3333-4444-555555555555"
META = "Текстовый гайд | python | Aug 22, 2026 | Advanced"
FIXTURE = Path(__file__).parent / "fixtures" / "boosty-text-post.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_api_fixture_renders_text_header_list_link_and_file_blocks():
    post = boosty_text.from_api(_fixture())
    assert post.title == "Текстовый гайд"
    assert post.body == "\n".join(
        [
            "Вступление к гайду.",
            "",
            "## Шаги",
            "",
            "- Сделать первое",
            "- Открыть второе (https://example.test/second)",
            "База знаний (https://example.test/base)",
            "[file: Чеклист.pdf (https://example.test/checklist.pdf, 1.5 MB)]",
            "",
            "Финал.",
        ]
    )


def test_link_and_file_urls_drop_query_parameters():
    link = "https://cdn.example.test/guide"
    file = "https://cdn.example.test/checklist.pdf"
    query = "?temporary=discard-me"
    lines = boosty_text.render_blocks(
        [
            {
                "type": "link",
                "url": link + query,
                "content": json.dumps([link + query, "", []]),
            },
            {
                "type": "file",
                "title": "Чеклист.pdf",
                "url": file + query,
                "size": 1048576,
            },
        ]
    )

    assert lines == [link, f"[file: Чеклист.pdf ({file}, 1.0 MB)]"]
    assert "?" not in "\n".join(lines)


def test_nested_list_items_are_rendered_recursively():
    text = lambda value: {  # noqa: E731 - compact form of a fixture JSON block
        "type": "text",
        "content": json.dumps([value, "", []]),
    }
    lines = boosty_text.render_blocks(
        [
            {
                "type": "list",
                "items": [
                    {
                        "data": [text("Top item")],
                        "items": [
                            {
                                "data": [text("Nested item")],
                                "items": [{"data": [text("Third level")]}],
                            }
                        ],
                    }
                ],
            }
        ]
    )

    assert lines == [
        "- Top item",
        "  - Nested item",
        "    - Third level",
    ]


def test_nested_media_block_is_still_rejected():
    data = _fixture()
    data["data"].append(
        {
            "type": "list",
            "items": [{"data": [{"type": "audio_file", "id": "nested-media"}]}],
        }
    )

    with pytest.raises(boosty_text.TextPostError, match="contains media"):
        boosty_text.from_api(data)


def test_fetch_uses_the_boosty_post_api_without_network(monkeypatch):
    from yt_dlp.extractor.boosty import BoostyIE

    seen = {}

    def download(self, url, post_id, **kwargs):
        seen.update(url=url, post_id=post_id, headers=kwargs["headers"])
        return _fixture()

    monkeypatch.setattr(BoostyIE, "_download_json", download)
    post = boosty_text.fetch(POST)

    assert post.title == "Текстовый гайд"
    assert seen == {
        "url": "https://api.boosty.to/v1/blog/author/post/11111111-2222-3333-4444-555555555555",
        "post_id": "11111111-2222-3333-4444-555555555555",
        "headers": {},
    }


def test_text_fallback_pause_keeps_the_harvest_script_bounds(monkeypatch):
    slept = []
    monkeypatch.setattr(boosty_text, "_uniform", lambda low, high: (low + high) / 2)
    monkeypatch.setattr(boosty_text, "_sleep", slept.append)
    boosty_text.pause_after_no_videos()
    assert slept == [5.5]


def test_text_api_uses_browser_cookies_without_a_cookie_or_token_file():
    params = boosty_text.ydl_params("firefox")
    assert params["cookiesfrombrowser"] == ("firefox",)
    assert not ({"cookiefile", "cookies", "token"} & set(params))


def test_api_post_with_media_is_not_misclassified_as_text_only():
    data = _fixture()
    data["data"].append({"type": "ok_video", "id": "video"})
    with pytest.raises(boosty_text.TextPostError, match="contains media"):
        boosty_text.from_api(data)


@pytest.mark.parametrize(
    "change, message",
    [
        ({"hasAccess": False}, "hasAccess=false"),
        ({"data": []}, "does not contain supported text"),
    ],
)
def test_inaccessible_or_empty_api_post_fails_closed(change, message):
    data = _fixture()
    data.update(change)
    with pytest.raises(boosty_text.TextPostError, match=message):
        boosty_text.from_api(data)


def test_no_videos_falls_back_to_text_and_uuid_dedup_skips_the_second_run(
    tmp_path, monkeypatch
):
    queue = tmp_path / "queue.txt"
    corpus_dir = tmp_path / "corpus"
    data = tmp_path / "data"
    queue.write_text(f"{POST}  {META}\n", encoding="utf-8")
    browsers = []

    def no_videos(*args, **kwargs):
        raise sources.SourceError("ERROR: No videos found")

    def fetch(url, browser):
        browsers.append(browser)
        return boosty_text.from_api(_fixture())

    monkeypatch.setattr(executor, "run_course", no_videos)
    monkeypatch.setattr(boosty_text, "fetch", fetch)
    monkeypatch.setattr(boosty_text, "pause_after_no_videos", lambda: None)
    result = CliRunner().invoke(
        app,
        ["corpus", str(queue), str(corpus_dir), "--data", str(data),
         "--cookies-from-browser", "firefox", "--asr", "dummy"],
    )

    assert result.exit_code == 0, result.output
    files = list(corpus_dir.glob("*.txt"))
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert body.startswith(
        f"# source: {POST}\n# metadata: {META}\n"
        "# type: Boosty text post\n\n"
    )
    assert "## Шаги" in body and "[file: Чеклист.pdf" in body
    assert browsers == ["firefox"]

    monkeypatch.setattr(
        executor,
        "run_course",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected run")),
    )
    monkeypatch.setattr(
        boosty_text,
        "fetch",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected API call")),
    )
    again = CliRunner().invoke(
        app,
        ["corpus", str(queue), str(corpus_dir), "--data", str(data),
         "--cookies-from-browser", "firefox", "--asr", "dummy"],
    )
    assert again.exit_code == 0, again.output
    assert len(list(corpus_dir.glob("*.txt"))) == 1

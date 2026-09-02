"""Course registry: the "acquired" record survives a disk cleanup."""

import shutil

from typer.testing import CliRunner

from coursedump import catalog
from coursedump.cli import app


runner = CliRunner()


def _dumped_course(root, name="course"):
    source = root / "in" / name
    source.mkdir(parents=True)
    (source / "one.txt").write_text("one", encoding="utf-8")
    (root / "queue.txt").write_text(f"{name}\n", encoding="utf-8")
    result = runner.invoke(app, ["queue", "run", "--data", str(root), "--asr", "dummy"])
    assert result.exit_code == 0, result.output
    return source


def test_catalog_keeps_manual_columns_and_the_row_after_source_is_deleted(tmp_path):
    root = tmp_path / "data"
    source = _dumped_course(root)

    assert runner.invoke(app, ["catalog", "--data", str(root)]).exit_code == 0
    path = root / "catalog.tsv"
    rows = catalog.read(path)
    assert rows["course"]["lessons"] == "1/1"
    assert rows["course"]["in"] == "yes"
    acquired = rows["course"]["acquired"]
    assert acquired != catalog.MISSING

    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "course\t-\t-\t-",
            "course\tНазвание\thttps://example.test/course\thttps://mirror.test/c",
        ),
        encoding="utf-8",
    )
    shutil.rmtree(source)

    assert runner.invoke(app, ["catalog", "--data", str(root)]).exit_code == 0
    rows = catalog.read(path)
    assert rows["course"]["title"] == "Название"
    assert rows["course"]["url"] == "https://example.test/course"
    assert rows["course"]["mirror"] == "https://mirror.test/c"
    assert rows["course"]["acquired"] == acquired  # written once, never lost
    assert rows["course"]["in"] == "no"
    assert rows["course"]["lessons"] == "1/1"  # counted from the snapshot, which is still there


def test_catalog_lists_a_downloaded_but_not_dumped_course(tmp_path):
    root = tmp_path / "data"
    (root / "in" / "svezhiy").mkdir(parents=True)
    (root / "in" / "svezhiy" / "lesson.txt").write_text("x", encoding="utf-8")

    assert runner.invoke(app, ["catalog", "--data", str(root)]).exit_code == 0
    rows = catalog.read(root / "catalog.tsv")
    assert rows["svezhiy"]["in"] == "yes"
    assert rows["svezhiy"]["lessons"] == catalog.MISSING


def test_catalog_row_survives_when_both_in_and_out_are_gone(tmp_path):
    root = tmp_path / "data"
    source = _dumped_course(root)
    assert runner.invoke(app, ["catalog", "--data", str(root)]).exit_code == 0
    shutil.rmtree(source)
    shutil.rmtree(root / "out" / "course")

    assert runner.invoke(app, ["catalog", "--data", str(root)]).exit_code == 0
    rows = catalog.read(root / "catalog.tsv")
    assert rows["course"]["in"] == "no"


def test_catalog_written_by_an_older_version_does_not_shift_columns(tmp_path):
    # The registry rows keep Russian titles and notes: they are real catalog
    # content, and multi-byte fields are where a column shift would show.
    # The file is read through its own header: a column added later must not
    # shift into its neighbour and overwrite hand-written fields with a date or
    # a lesson counter.
    root = tmp_path / "data"
    root.mkdir(parents=True)
    (root / "catalog.tsv").write_text(
        "slug\ttitle\turl\tacquired\tlessons\tmedia\tin\tnote\n"
        "staryy\tСтарый курс\thttps://example.test/s\t2026-01-02\t7/7\t1.0 GB\tнет\tзаметка\n",
        encoding="utf-8",
    )

    assert runner.invoke(app, ["catalog", "--data", str(root)]).exit_code == 0
    rows = catalog.read(root / "catalog.tsv")
    assert rows["staryy"]["title"] == "Старый курс"
    assert rows["staryy"]["url"] == "https://example.test/s"
    assert rows["staryy"]["acquired"] == "2026-01-02"
    assert rows["staryy"]["note"] == "заметка"
    assert rows["staryy"]["mirror"] in ("", catalog.MISSING)


def test_catalog_skips_remote_posts(tmp_path):
    root = tmp_path / "data"
    post = root / "out" / "post"
    post.mkdir(parents=True)
    (post / "source.json").write_text(
        '{"adapter": "ytdlp", "url": "https://example.test/p/1"}', encoding="utf-8"
    )

    assert runner.invoke(app, ["catalog", "--data", str(root)]).exit_code == 0
    assert catalog.read(root / "catalog.tsv") == {}

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from prodagent.tooling.builtin._atomic import atomic_write_text
from prodagent.tooling.builtin.edit import make_edit
from prodagent.tooling.builtin.write import make_write

if TYPE_CHECKING:
    from pathlib import Path


class TestAtomicWriteText:
    def test_original_intact_when_os_replace_fails(self, tmp_path):
        path = tmp_path / "target.txt"
        path.write_text("ORIGINAL", encoding="utf-8")

        with (
            patch("prodagent.tooling.builtin._atomic.os.replace", side_effect=OSError("disk full")),
            pytest.raises(OSError),
        ):
            atomic_write_text(path, "NEW CONTENT THAT IS LONGER")

        assert path.read_text(encoding="utf-8") == "ORIGINAL"

    def test_tmp_file_cleaned_up_on_success(self, tmp_path):
        path = tmp_path / "out.txt"
        atomic_write_text(path, "hello")
        assert path.read_text(encoding="utf-8") == "hello"
        tmp = path.with_suffix(path.suffix + ".tmp")
        assert not tmp.exists(), f"tmp file {tmp} should not exist after success"

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "nested" / "deep" / "out.txt"
        atomic_write_text(path, "deep content")
        assert path.read_text(encoding="utf-8") == "deep content"

    def test_overwrites_existing_file(self, tmp_path):
        path = tmp_path / "out.txt"
        path.write_text("OLD", encoding="utf-8")
        atomic_write_text(path, "NEW")
        assert path.read_text(encoding="utf-8") == "NEW"


class TestEditToolCrashSafety:
    async def test_edit_original_intact_when_replace_fails(self, tmp_path):
        path = tmp_path / "edit.txt"
        path.write_text("line one\nline two\nline three\n", encoding="utf-8")

        seen: set[Path] = {path}
        edit = make_edit(seen_paths=seen, allowed_dirs=[tmp_path])

        with patch("prodagent.tooling.builtin._atomic.os.replace", side_effect=OSError("EIO")):
            result = await edit(
                file_path=str(path),
                old_string="line two",
                new_string="line TWO",
            )

        assert result.error is not None
        assert result.error.code == "file_write_error"
        assert "Error writing file" in result.error.message
        assert path.read_text(encoding="utf-8") == "line one\nline two\nline three\n"

    async def test_edit_writes_atomically_on_success(self, tmp_path):
        path = tmp_path / "edit.txt"
        path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

        seen: set[Path] = {path}
        edit = make_edit(seen_paths=seen, allowed_dirs=[tmp_path])

        result = await edit(
            file_path=str(path),
            old_string="beta",
            new_string="BETA",
        )

        assert result.to_wire()["ok"] is True
        assert path.read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"
        tmp = path.with_suffix(path.suffix + ".tmp")
        assert not tmp.exists()


class TestWriteToolCrashSafety:
    async def test_write_original_intact_when_replace_fails(self, tmp_path):
        path = tmp_path / "target.txt"
        path.write_text("ORIGINAL", encoding="utf-8")

        write = make_write(allowed_dirs=[tmp_path])

        with patch("prodagent.tooling.builtin._atomic.os.replace", side_effect=OSError("ENOSPC")):
            result = await write(file_path=str(path), content="NEW")

        assert result.error is not None
        assert result.error.code == "file_write_error"
        assert "Error writing file" in result.error.message
        assert path.read_text(encoding="utf-8") == "ORIGINAL"

    async def test_write_new_file_uses_atomic_path(self, tmp_path):
        path = tmp_path / "fresh.txt"
        write = make_write(allowed_dirs=[tmp_path])

        result = await write(file_path=str(path), content="fresh content")

        assert result.to_wire()["ok"] is True
        assert path.read_text(encoding="utf-8") == "fresh content"
        tmp = path.with_suffix(path.suffix + ".tmp")
        assert not tmp.exists()

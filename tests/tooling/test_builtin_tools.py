from __future__ import annotations

import asyncio
from pathlib import Path

from prodagent.tooling.builtin import (
    make_builtin_fs_bundle,
    make_edit,
    make_read,
    make_write,
)


class TestReadTool:
    async def test_read_text_file_with_line_numbers(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("line one\nline two\nline three\n")
        tool = make_read(allowed_dirs=[tmp_path])
        result = await tool(file_path=str(f))
        wire = result.to_wire()
        assert wire["ok"]
        assert "1\tline one" in wire["content"]
        assert "2\tline two" in wire["content"]
        assert wire["total_lines"] == 3

    async def test_read_nonexistent_file(self, tmp_path):
        tool = make_read(allowed_dirs=[tmp_path])
        result = await tool(file_path=str(tmp_path / "nope.txt"))
        assert result.error is not None
        assert result.error.code == "file_not_found"

    async def test_read_path_outside_allowed_dirs(self, tmp_path):
        tool = make_read(allowed_dirs=[tmp_path])
        result = await tool(file_path="/etc/passwd")
        assert result.error is not None
        assert result.error.code == "path_not_allowed"

    async def test_read_pagination_with_offset(self, tmp_path):
        f = tmp_path / "lines.txt"
        f.write_text("\n".join(f"line {i}" for i in range(100)))
        tool = make_read(allowed_dirs=[tmp_path])
        result = await tool(file_path=str(f), offset=50, limit=10)
        wire = result.to_wire()
        assert wire["ok"]
        assert wire["offset"] == 50
        assert "51\tline 50" in wire["content"]

    async def test_read_partial_view_notice(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_text("\n".join(f"line {i}" for i in range(500)))
        tool = make_read(allowed_dirs=[tmp_path])
        result = await tool(file_path=str(f), limit=10)
        wire = result.to_wire()
        assert wire["is_partial"]
        assert "PARTIAL view" in wire["content"]
        assert "offset=10" in wire["content"]

    async def test_read_image_returns_base64(self, tmp_path):
        f = tmp_path / "img.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        tool = make_read(allowed_dirs=[tmp_path])
        result = await tool(file_path=str(f))
        wire = result.to_wire()
        assert wire["ok"]
        assert wire["is_image"]
        assert "base64" in wire["content"]

    async def test_default_allowed_dirs_is_cwd(self, tmp_path, monkeypatch):
        """No allowlist configured collapses to cwd — least privilege by default."""
        inside = tmp_path / "inside.txt"
        inside.write_text("hello")
        outside = tmp_path.parent / "outside-probe.txt"
        outside.write_text("secret")

        monkeypatch.chdir(tmp_path)
        tool = make_read(allowed_dirs=None)
        result = await tool(file_path=str(inside))
        assert result.to_wire()["ok"]

        blocked = await tool(file_path=str(outside))
        assert blocked.error is not None
        assert blocked.error.code == "path_not_allowed"
        outside.unlink()

    async def test_explicit_allowed_dirs_widen_access(self, tmp_path):
        other = tmp_path.parent / "widen-probe.txt"
        other.write_text("wider")
        try:
            tool = make_read(allowed_dirs=[tmp_path, tmp_path.parent])
            result = await tool(file_path=str(other))
            assert result.to_wire()["ok"]
        finally:
            other.unlink()


class TestEditTool:
    async def test_edit_after_read_succeeds(self, tmp_path):
        f = tmp_path / "edit.txt"
        f.write_text("hello world\nfoo bar\n")
        seen: set[Path] = set()
        read_tool = make_read(allowed_dirs=[tmp_path], seen_paths=seen)
        edit_tool = make_edit(seen_paths=seen, allowed_dirs=[tmp_path])

        await read_tool(file_path=str(f))
        result = await edit_tool(file_path=str(f), old_string="hello world", new_string="hi earth")
        assert result.to_wire()["ok"]
        assert "hi earth" in f.read_text()

    async def test_edit_without_read_fails(self, tmp_path):
        f = tmp_path / "unread.txt"
        f.write_text("content")
        edit_tool = make_edit(seen_paths=set(), allowed_dirs=[tmp_path])
        result = await edit_tool(file_path=str(f), old_string="content", new_string="changed")
        assert result.error is not None
        assert result.error.code == "read_before_edit_required"

    async def test_edit_old_string_not_found(self, tmp_path):
        f = tmp_path / "find.txt"
        f.write_text("the quick brown fox")
        seen = {f}
        edit_tool = make_edit(seen_paths=seen, allowed_dirs=[tmp_path])
        result = await edit_tool(file_path=str(f), old_string="nonexistent", new_string="x")
        assert result.error is not None
        assert result.error.code == "old_string_not_found"

    async def test_edit_non_unique_old_string_without_replace_all(self, tmp_path):
        f = tmp_path / "dup.txt"
        f.write_text("foo bar foo bar")
        seen = {f}
        edit_tool = make_edit(seen_paths=seen, allowed_dirs=[tmp_path])
        result = await edit_tool(file_path=str(f), old_string="foo", new_string="baz")
        assert result.error is not None
        assert result.error.code == "old_string_not_unique"
        assert "2 times" in result.error.message

    async def test_edit_replace_all(self, tmp_path):
        f = tmp_path / "dup.txt"
        f.write_text("foo bar foo bar")
        seen = {f}
        edit_tool = make_edit(seen_paths=seen, allowed_dirs=[tmp_path])
        result = await edit_tool(
            file_path=str(f), old_string="foo", new_string="baz", replace_all=True
        )
        wire = result.to_wire()
        assert wire["ok"]
        assert wire["replacements"] == 2
        assert f.read_text() == "baz bar baz bar"

    async def test_edit_path_outside_allowed(self, tmp_path):
        f = tmp_path / "out.txt"
        f.write_text("content")
        seen = {f}
        edit_tool = make_edit(seen_paths=seen, allowed_dirs=[Path("/other")])
        result = await edit_tool(file_path=str(f), old_string="content", new_string="x")
        assert result.error is not None
        assert result.error.code == "path_not_allowed"


class TestWriteTool:
    async def test_write_creates_new_file(self, tmp_path):
        f = tmp_path / "new.txt"
        tool = make_write(allowed_dirs=[tmp_path])
        result = await tool(file_path=str(f), content="hello world")
        wire = result.to_wire()
        assert wire["ok"]
        assert f.read_text() == "hello world"
        assert wire["chars_written"] == 11

    async def test_write_overwrites_existing(self, tmp_path):
        f = tmp_path / "existing.txt"
        f.write_text("old content")
        tool = make_write(allowed_dirs=[tmp_path])
        result = await tool(file_path=str(f), content="new content")
        assert result.to_wire()["ok"]
        assert f.read_text() == "new content"

    async def test_write_creates_parent_dirs(self, tmp_path):
        f = tmp_path / "subdir" / "nested" / "file.txt"
        tool = make_write(allowed_dirs=[tmp_path])
        result = await tool(file_path=str(f), content="deep")
        assert result.to_wire()["ok"]
        assert f.read_text() == "deep"

    async def test_write_path_outside_allowed(self, tmp_path):
        tool = make_write(allowed_dirs=[tmp_path])
        result = await tool(file_path="/etc/prodagent_test", content="x")
        assert result.error is not None
        assert result.error.code == "path_not_allowed"


class TestFsBundle:
    async def test_bundle_shares_seen_paths(self, tmp_path):
        f = tmp_path / "bundled.txt"
        f.write_text("original text")
        read, edit, write = make_builtin_fs_bundle(allowed_dirs=[tmp_path])

        await read(file_path=str(f))
        result = await edit(file_path=str(f), old_string="original", new_string="modified")
        assert result.to_wire()["ok"]
        assert "modified text" in f.read_text()

    async def test_bundle_edit_without_read_fails(self, tmp_path):
        f = tmp_path / "unread.txt"
        f.write_text("content")
        read, edit, write = make_builtin_fs_bundle(allowed_dirs=[tmp_path])
        result = await edit(file_path=str(f), old_string="content", new_string="x")
        assert result.error is not None
        assert result.error.code == "read_before_edit_required"

    async def test_concurrent_edits_same_file_no_lost_update(self, tmp_path):
        """Two concurrent edits to the same path must each see the other's write.

        Regression for the lost-update: the read-modify-write used to run outside
        any per-path lock, so both edits read the same base and one clobbered the
        other.
        """
        f = tmp_path / "concurrent.txt"
        f.write_text("alpha beta gamma")
        read, edit, write = make_builtin_fs_bundle(allowed_dirs=[tmp_path])
        await read(file_path=str(f))

        r1, r2 = await asyncio.gather(
            edit(file_path=str(f), old_string="alpha", new_string="ALPHA"),
            edit(file_path=str(f), old_string="gamma", new_string="GAMMA"),
        )
        assert r1.to_wire()["ok"]
        assert r2.to_wire()["ok"]
        content = f.read_text()
        assert "ALPHA" in content
        assert "GAMMA" in content


class TestCheckPath:
    def test_empty_allowlist_denies_all(self, tmp_path):
        from prodagent.tooling.builtin._atomic import check_path

        # Empty allowlist is a misconfiguration, not "anywhere" — deny all.
        assert check_path(tmp_path / "f.txt", []) is None

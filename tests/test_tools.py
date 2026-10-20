import tempfile
import os
from pathlib import Path

import pytest

import app.tools as tools_mod
from app.tools import TOOL_SCHEMAS, ToolContext, _is_blocked_url, _safe_path, make_executor


@pytest.fixture
def workspace(tmp_path: Path) -> str:
    (tmp_path / "a.txt").write_text("hello world\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("needle here\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    return str(tmp_path)


@pytest.fixture
def run(workspace: str):
    ctx = ToolContext(workspace=workspace)
    ex = make_executor(ctx)
    return ctx, ex


def call(ex, name: str, **args) -> str:
    return ex(name, args)


def test_schemas_are_valid_tool_definitions():
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert {"list_files", "read_file", "search", "write_file", "edit_file",
            "run_command", "web_search", "web_fetch", "review_comment", "done"} <= names


def test_read_file(run):
    ctx, ex = run
    assert call(ex, "read_file", path="a.txt") == "hello world\n"


def test_read_file_missing(run):
    ctx, ex = run
    assert "error" in call(ex, "read_file", path="nope.txt")


def test_safe_path_blocks_traversal(tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("pwn", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(ValueError):
        _safe_path(str(ws), "../secret.txt")
    with pytest.raises(ValueError):
        _safe_path(str(ws), "sub/../../secret.txt")


def test_read_file_rejects_escape(run):
    ctx, ex = run
    with pytest.raises(ValueError):
        call(ex, "read_file", path="../secret.txt")


def test_list_files_excludes_git_dir(run):
    ctx, ex = run
    out = call(ex, "list_files", path=".")
    assert "a.txt" in out
    assert ".git" not in out
    assert out.count("b.txt") == 1


def test_list_files_not_a_directory(run):
    ctx, ex = run
    assert "error" in call(ex, "list_files", path="a.txt")


def test_search_finds_with_relative_paths(run):
    ctx, ex = run
    out = call(ex, "search", pattern="needle")
    assert out.startswith(os.path.join("sub", "b.txt"))
    assert "1:needle here" in out


def test_search_bad_regex(run):
    ctx, ex = run
    assert "error: invalid regex" in call(ex, "search", pattern="(")


def test_write_file_creates_nested(tmp_path):
    ctx = ToolContext(workspace=str(tmp_path))
    ex = make_executor(ctx)
    out = call(ex, "write_file", path="deep/dir/ok.txt", content="data")
    assert "wrote" in out and "ok.txt" in out
    assert (tmp_path / "deep" / "dir" / "ok.txt").read_text(encoding="utf-8") == "data"


def test_edit_file_unique_replacement(run):
    ctx, ex = run
    out = call(ex, "edit_file", path="a.txt", old_string="world", new_string="there")
    assert out == "edited a.txt"
    assert (Path(ctx.workspace) / "a.txt").read_text(encoding="utf-8") == "hello there\n"


def test_edit_file_rejects_ambiguous_match(run):
    ctx, ex = run
    call(ex, "write_file", path="d.txt", content="x x x")
    out = call(ex, "edit_file", path="d.txt", old_string="x", new_string="y")
    assert "found 3 times" in out


def test_edit_file_missing_old_string(run):
    ctx, ex = run
    assert "error: old_string not found" in call(
        ex, "edit_file", path="a.txt", old_string="zzz", new_string="y"
    )


def test_run_command_captures_output_and_exit_code(run):
    ctx, ex = run
    out = call(ex, "run_command", command="echo ping")
    assert "ping" in out and "[exit 0]" in out
    code = call(ex, "run_command", command="python -c \"raise SystemExit(3)\"")
    assert "[exit 3]" in code
    assert ctx.commands_run == 2


def test_service_tools_covered_by_executor_errors(run):
    ctx, ex = run
    assert "error: unknown tool" in call(ex, "bogus_tool")


def test_done_sets_context(run):
    ctx, ex = run
    out = call(ex, "done", summary="all finished")
    assert out.startswith("DONE:")
    assert ctx.done is True
    assert ctx.summary == "all finished"


def test_review_comment_queues_inline(run):
    ctx, ex = run
    out = call(ex, "review_comment", path="src/x.py", line=4, body="bug here")
    assert "queued inline comment" in out
    assert ctx.inline_comments == [{"path": "src/x.py", "line": 4, "body": "bug here"}]


def test_review_comment_validation(run):
    ctx, ex = run
    assert "error" in call(ex, "review_comment", path="a", line=0, body="b")
    assert "error" in call(ex, "review_comment", path="a", line="x", body="b")


def test_truncate():
    out = tools_mod._truncate("x" * (tools_mod.MAX_OUTPUT + 100))
    assert out.endswith("chars]") and len(out) < tools_mod.MAX_OUTPUT + 200

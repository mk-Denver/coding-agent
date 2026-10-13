"""Tool definitions and a workspace-scoped executor for the agent loop."""
from __future__ import annotations

import fnmatch
import os
import re
import signal
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

from .logger import get_logger

log = get_logger("tools")

# Tool JSON schemas (OpenAI function-calling format).
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files under a directory in the repo workspace. Returns relative paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative directory path, '.' for root."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full contents of a file in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search file contents with a regex pattern. Returns matching lines with file:line.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regex pattern."},
                    "glob": {"type": "string", "description": "Optional file glob, e.g. '*.py'."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write full contents to a file (creates or overwrites).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace a unique occurrence of old_string with new_string in a file. Use enough context to be unique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command in the workspace (e.g. lint, tests, build). Output is truncated.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Full shell command line."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Signal that the task is complete. Provide a concise summary of what you did.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
    },
]

MAX_OUTPUT = 4000


@dataclass
class ToolContext:
    workspace: str
    done: bool = False
    summary: str = ""
    commands_run: int = 0


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n...[truncated {len(text) - MAX_OUTPUT} chars]"


def _safe_path(workspace: str, rel: str) -> str:
    abs_workspace = os.path.realpath(workspace)
    rel = rel.lstrip("/").lstrip("\\")
    full = os.path.realpath(os.path.join(abs_workspace, rel))
    if full != abs_workspace and not full.startswith(abs_workspace + os.sep):
        raise ValueError("path escapes workspace")
    return full


def make_executor(ctx: ToolContext) -> Callable[[str, dict[str, Any]], str]:
    def execute(name: str, args: dict[str, Any]) -> str:
        if name == "list_files":
            rel = args.get("path", ".")
            base = _safe_path(ctx.workspace, rel)
            if not os.path.isdir(base):
                return f"error: not a directory: {rel}"
            out: list[str] = []
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv"}]
                for f in files:
                    full = os.path.join(root, f)
                    out.append(os.path.relpath(full, base))
            return _truncate("\n".join(sorted(out)[:500])) or "(empty)"

        if name == "read_file":
            rel = args.get("path")
            if not rel:
                return "error: path is required"
            full = _safe_path(ctx.workspace, rel)
            if not os.path.isfile(full):
                return f"error: file not found: {rel}"
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                return _truncate(fh.read())

        if name == "search":
            pattern = args.get("pattern")
            if not pattern:
                return "error: pattern is required"
            glob = args.get("glob")
            try:
                rx = re.compile(pattern)
            except re.error as exc:
                return f"error: invalid regex: {exc}"
            hits: list[str] = []
            abs_ws = os.path.realpath(ctx.workspace)
            for root, dirs, files in os.walk(ctx.workspace):
                dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv"}]
                for f in files:
                    if glob and not fnmatch.fnmatch(f, glob):
                        continue
                    full = os.path.realpath(os.path.join(root, f))
                    if full != abs_ws and not full.startswith(abs_ws + os.sep):
                        continue
                    try:
                        with open(full, "r", encoding="utf-8", errors="replace") as fh:
                            for i, line in enumerate(fh, 1):
                                if rx.search(line):
                                    rel = os.path.relpath(full, ctx.workspace)
                                    hits.append(f"{rel}:{i}:{line.rstrip()}")
                    except Exception:
                        continue
                if len(hits) > 200:
                    break
            return _truncate("\n".join(hits[:200])) or "(no matches)"

        if name == "write_file":
            rel = args.get("path")
            if not rel:
                return "error: path is required"
            content = args.get("content")
            if content is None:
                return "error: content is required"
            full = _safe_path(ctx.workspace, rel)
            os.makedirs(os.path.dirname(full) or ctx.workspace, exist_ok=True)
            tmp = full + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(content)
                os.replace(tmp, full)
            except Exception:
                if os.path.exists(tmp):
                    os.remove(tmp)
                raise
            return f"wrote {rel} ({len(content)} bytes)"

        if name == "edit_file":
            rel = args.get("path")
            if not rel:
                return "error: path is required"
            old = args.get("old_string")
            new = args.get("new_string")
            if old is None:
                return "error: old_string is required"
            if new is None:
                return "error: new_string is required"
            full = _safe_path(ctx.workspace, rel)
            if not os.path.isfile(full):
                return f"error: file not found: {rel}"
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
            count = content.count(old)
            if count == 0:
                return "error: old_string not found"
            if count > 1:
                return f"error: old_string found {count} times; provide more context to make it unique"
            content = content.replace(old, new, 1)
            tmp = full + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(content)
                os.replace(tmp, full)
            except Exception:
                if os.path.exists(tmp):
                    os.remove(tmp)
                raise
            return f"edited {rel}"

        if name == "run_command":
            cmd = args.get("command")
            if not cmd:
                return "error: command is required"
            ctx.commands_run += 1
            # shell=True is intentional: the agent needs shell semantics (pipes, &&)
            # for lint/test commands. Mitigations: restricted env (no secrets),
            # cwd confined to workspace, start_new_session for clean process-group
            # kill on timeout. Do NOT run this tool without these boundaries.
            restricted_env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": ctx.workspace,
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                "TMPDIR": ctx.workspace,
            }
            try:
                proc = subprocess.Popen(
                    cmd,
                    shell=True,
                    cwd=ctx.workspace,
                    env=restricted_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                try:
                    stdout, stderr = proc.communicate(timeout=120)
                    out = (stdout or "") + (("\n[stderr]\n" + stderr) if stderr else "")
                    return f"[exit {proc.returncode}]\n{_truncate(out)}"
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except OSError:
                        proc.kill()
                    try:
                        proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    return "error: command timed out (120s)"
            except Exception as exc:  # noqa: BLE001
                return f"error: failed to run command: {exc}"

        if name == "done":
            ctx.done = True
            ctx.summary = args.get("summary", "")
            return f"DONE: {ctx.summary}"

        return f"error: unknown tool {name}"

    return execute

"""Tool definitions and a workspace-scoped executor for the agent loop."""
from __future__ import annotations

import fnmatch
import html
import os
import re
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote, urlparse

import httpx

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
            "name": "web_search",
            "description": "Search the web via DuckDuckGo. Returns up to 8 results with title, URL, and snippet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch the content of a URL and return it as text (HTML stripped to readable text, truncated).",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL to fetch."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "review_comment",
            "description": "Add an inline review comment on a specific file and line of the PR diff. Use for pointing out specific issues at exact locations. Only works during PR review.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path in the repo, e.g. 'src/main.py'."},
                    "line": {"type": "integer", "description": "Line number in the file to comment on."},
                    "body": {"type": "string", "description": "The review comment text (markdown)."},
                },
                "required": ["path", "line", "body"],
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

_BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]",
                   "169.254.169.254", "metadata.google.internal"}


def _is_blocked_url(url: str) -> bool:
    """Block SSRF targets: localhost, link-local, private ranges, cloud metadata."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if host in _BLOCKED_HOSTS:
            return True
        if host.endswith(".local") or host.endswith(".internal"):
            return True
        import ipaddress
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return True
        except ValueError:
            pass
        return False
    except Exception:
        return True


@dataclass
class ToolContext:
    workspace: str
    done: bool = False
    summary: str = ""
    commands_run: int = 0
    inline_comments: list = None

    def add_inline_comment(self, path: str, line: int, body: str) -> None:
        if self.inline_comments is None:
            self.inline_comments = []
        self.inline_comments.append({"path": path, "line": line, "body": body})


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
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(full) or ctx.workspace, prefix=".agent-")
            os.close(fd)
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
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(full) or ctx.workspace, prefix=".agent-")
            os.close(fd)
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
                "TMPDIR": os.path.join(ctx.workspace, ".agent-tmp"),
            }
            os.makedirs(restricted_env["TMPDIR"], exist_ok=True)
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
                    except ProcessLookupError:
                        pass
                    except OSError:
                        proc.kill()
                    try:
                        proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    return "error: command timed out (120s)"
            except Exception as exc:  # noqa: BLE001
                return f"error: failed to run command: {exc}"

        if name == "web_search":
            query = args.get("query")
            if not query:
                return "error: query is required"
            try:
                with httpx.Client(timeout=30, follow_redirects=True) as c:
                    r = c.get(
                        "https://html.duckduckgo.com/html/",
                        params={"q": query},
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                    r.raise_for_status()
                results: list[str] = []
                for m in re.finditer(
                    r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
                    r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
                    r.text, re.DOTALL,
                ):
                    url = m.group(1)
                    title = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
                    snippet = html.unescape(re.sub(r"<[^>]+>", "", m.group(3))).strip()
                    if url.startswith("//"):
                        url = "https:" + url
                    results.append(f"{title}\n  {url}\n  {snippet}")
                    if len(results) >= 8:
                        break
                return _truncate("\n\n".join(results)) or "(no results)"
            except Exception as exc:  # noqa: BLE001
                return f"error: web_search failed: {exc}"

        if name == "web_fetch":
            url = args.get("url")
            if not url:
                return "error: url is required"
            if not re.match(r"^https?://", url):
                return "error: url must start with http:// or https://"
            if _is_blocked_url(url):
                return "error: fetching this host is not allowed"
            try:
                with httpx.Client(timeout=30, follow_redirects=True) as c:
                    r = c.get(url, headers={"User-Agent": "Mozilla/5.0"})
                    r.raise_for_status()
                text = r.text
                text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
                text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
                text = re.sub(r"<[^>]+>", " ", text)
                text = html.unescape(text)
                text = re.sub(r"\s+", " ", text).strip()
                return _truncate(text)
            except Exception as exc:  # noqa: BLE001
                return f"error: web_fetch failed: {exc}"

        if name == "review_comment":
            path = args.get("path")
            line = args.get("line")
            body = args.get("body")
            if not path or not body or not line:
                return "error: path, line, and body are required"
            try:
                line = int(line)
            except (TypeError, ValueError):
                return "error: line must be an integer"
            ctx.add_inline_comment(path, line, body)
            return f"queued inline comment on {path}:{line}"

        if name == "done":
            ctx.done = True
            ctx.summary = args.get("summary", "")
            return f"DONE: {ctx.summary}"

        return f"error: unknown tool {name}"

    return execute

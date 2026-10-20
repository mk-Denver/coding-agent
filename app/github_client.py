"""GitHub operations: clone, branch, commit, push, PRs, comments, diffs."""
from __future__ import annotations

import base64
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings
from .logger import get_logger

log = get_logger("github")

API = "https://api.github.com"

_CRED_RE = re.compile(r"https?://[^@]+@")


@dataclass
class PrInfo:
    number: int
    title: str
    body: str
    head_ref: str
    base_ref: str
    diff: str
    author: str
    head_sha: str


class GitHubClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.token = settings.github_token
        self.http = httpx.Client(
            base_url=API,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "personal-coding-agent",
            },
            timeout=60,
        )

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- repo metadata -------------------------------------------------
    def parse_repo(self, full_name: str) -> tuple[str, str]:
        parts = full_name.split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(f"invalid repo full_name: {full_name!r}")
        return parts[0], parts[1]

    def repo_url(self, full_name: str) -> str:
        return f"https://github.com/{full_name}.git"

    def _make_git_env(self) -> tuple[dict[str, str], str]:
        """Build a per-operation env with a GIT_ASKPASS helper that feeds the
        token without embedding it in the URL.

        Returns (env, askpass_path); the caller must delete askpass_path in a
        finally block. State is not kept on self so concurrent git operations
        never share (and clobber) one credential helper file.
        """
        askpass = tempfile.NamedTemporaryFile(
            mode="w", suffix=".sh", prefix="askpass_", delete=False
        )
        try:
            askpass.write("#!/bin/sh\n")
            askpass.write(f"echo {shlex.quote(self.token)}\n")
            askpass.close()
            os.chmod(askpass.name, 0o700)
        except Exception:
            askpass.close()
            if os.path.exists(askpass.name):
                os.remove(askpass.name)
            raise
        return {"GIT_ASKPASS": askpass.name, "GIT_TERMINAL_PROMPT": "0"}, askpass.name

    @staticmethod
    def _rm(path: str) -> None:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    # ---- issues / PRs / comments ---------------------------------------
    def get_issue(self, full_name: str, number: int) -> dict[str, Any]:
        r = self.http.get(f"/repos/{full_name}/issues/{number}")
        r.raise_for_status()
        return r.json()

    def get_issue_comments(self, full_name: str, number: int) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        page = 1
        while True:
            r = self.http.get(
                f"/repos/{full_name}/issues/{number}/comments",
                params={"per_page": 100, "page": page},
            )
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            comments.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return comments

    def post_comment(self, full_name: str, number: int, body: str) -> None:
        r = self.http.post(
            f"/repos/{full_name}/issues/{number}/comments",
            json={"body": body},
        )
        r.raise_for_status()
        log.info("Posted comment on %s#%d", full_name, number)

    def post_review(
        self,
        full_name: str,
        number: int,
        body: str,
        head_sha: str,
        inline_comments: list[dict] | None = None,
        event: str = "COMMENT",
    ) -> str:
        """Post a PR review with optional inline line-level comments.

        inline_comments: list of {"path": str, "line": int, "body": str}
        """
        payload: dict[str, Any] = {
            "body": body,
            "event": event,
        }
        if head_sha:
            payload["commit_id"] = head_sha
        if inline_comments:
            payload["comments"] = [
                {
                    "path": c["path"],
                    "line": c["line"],
                    "side": "RIGHT",
                    "body": c["body"],
                }
                for c in inline_comments
            ]
        r = self.http.post(
            f"/repos/{full_name}/pulls/{number}/reviews",
            json=payload,
        )
        r.raise_for_status()
        url = r.json().get("html_url", "")
        log.info("Posted review on %s#%d (%d inline comments)", full_name, number, len(inline_comments or []))
        return url

    def get_pr(self, full_name: str, number: int) -> PrInfo:
        r = self.http.get(f"/repos/{full_name}/pulls/{number}")
        r.raise_for_status()
        d = r.json()
        diff_url = d.get("diff_url")
        if diff_url:
            diff_r = self.http.get(diff_url)
        else:
            diff_r = self.http.get(
                f"/repos/{full_name}/pulls/{number}",
                headers={"Accept": "application/vnd.github.diff"},
            )
        diff_r.raise_for_status()
        return PrInfo(
            number=number,
            title=d.get("title", ""),
            body=d.get("body", "") or "",
            head_ref=d.get("head", {}).get("ref", ""),
            base_ref=d.get("base", {}).get("ref", ""),
            diff=diff_r.text,
            author=d.get("user", {}).get("login", ""),
            head_sha=d.get("head", {}).get("sha", ""),
        )

    def list_pr_files(self, full_name: str, number: int) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        page = 1
        while True:
            r = self.http.get(
                f"/repos/{full_name}/pulls/{number}/files",
                params={"per_page": 100, "page": page},
            )
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            files.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return files

    def get_file(self, full_name: str, path: str, ref: str | None = None) -> str:
        params = {"ref": ref} if ref else {}
        r = self.http.get(f"/repos/{full_name}/contents/{quote(path)}", params=params)
        r.raise_for_status()
        data = r.json()
        if data.get("type") != "file":
            raise RuntimeError(f"unexpected content type for {path}: {data.get('type')}")
        content = data.get("content", "")
        if data.get("encoding") == "base64":
            return base64.b64decode(content).decode("utf-8", errors="replace")
        return content

    # ---- git operations (clone/branch/commit/push) ---------------------
    def clone(
        self, full_name: str, base_ref: str, branch_name: str
    ) -> str:
        """Clone base_ref into a temp dir, create+checkout branch_name. Returns path."""
        workspace = tempfile.mkdtemp(prefix="agent_")
        url = self.repo_url(full_name)
        git_env, askpass = self._make_git_env()
        try:
            self._run(["git", "clone", "--quiet", "--depth", "1", "--branch", base_ref, url, workspace], env=git_env)
            self._run(["git", "-C", workspace, "checkout", "-b", branch_name])
            self._run(["git", "-C", workspace, "config", "user.name", "coding-agent"])
            self._run(["git", "-C", workspace, "config", "user.email", "agent@local"])
            self._add_git_excludes(workspace)
        except Exception:
            shutil.rmtree(workspace, ignore_errors=True)
            raise
        finally:
            self._rm(askpass)
        log.info("Cloned %s@%s -> %s (branch %s)", full_name, base_ref, workspace, branch_name)
        return workspace

    @staticmethod
    def _add_git_excludes(workspace: str) -> None:
        exclude = os.path.join(workspace, ".git", "info", "exclude")
        try:
            with open(exclude, "a", encoding="utf-8") as fh:
                fh.write("\n.agent-tmp/\n")
        except Exception as e:
            log.warning("could not write git excludes in %s: %s", workspace, e)

    def commit_all(self, workspace: str, message: str) -> bool:
        self._run(["git", "-C", workspace, "add", "-A"])
        status = self._run(["git", "-C", workspace, "status", "--porcelain"])
        if not status.strip():
            return False
        self._run(["git", "-C", workspace, "commit", "-m", message, "--quiet"])
        return True

    def push(self, full_name: str, workspace: str, branch_name: str) -> None:
        url = self.repo_url(full_name)
        git_env, askpass = self._make_git_env()
        try:
            self._run(
                ["git", "-C", workspace, "push", "--quiet", url, f"HEAD:{branch_name}"],
                env=git_env,
            )
        finally:
            self._rm(askpass)

    def create_pr(
        self, full_name: str, head: str, base: str, title: str, body: str
    ) -> str:
        r = self.http.post(
            f"/repos/{full_name}/pulls",
            json={"title": title, "body": body, "head": head, "base": base},
        )
        r.raise_for_status()
        return r.json().get("html_url", "")

    # ---- cleanup -------------------------------------------------------
    def cleanup(self, workspace: str) -> None:
        try:
            shutil.rmtree(workspace, ignore_errors=True)
        except Exception as e:
            log.warning("cleanup failed for %s: %s", workspace, e)

    # ---- helpers -------------------------------------------------------
    def _redact(self, text: str) -> str:
        text = _CRED_RE.sub("https://***@", text)
        if self.token:
            text = text.replace(self.token, "***")
        return text

    def _run(self, cmd: list[str], env: dict | None = None) -> str:
        safe_cmd = [self._redact(c) for c in cmd]
        log.debug("run: %s", " ".join(safe_cmd))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env={**os.environ, **(env or {})},
            timeout=120,
        )
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            err = self._redact(err)
            raise RuntimeError(f"command failed ({safe_cmd[0]}): {err}")
        return result.stdout

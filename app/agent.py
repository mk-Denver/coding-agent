"""Agent orchestration: review PRs, fix issues, answer questions on issues."""
from __future__ import annotations

import json
import time
import uuid
from typing import Callable

from .config import Settings
from .github_client import GitHubClient, PrInfo
from .llm import chat, make_client
from .logger import get_logger
from .tools import TOOL_SCHEMAS, ToolContext, make_executor

log = get_logger("agent")

NotifyFn = Callable[[str], None]

BASE_SYSTEM = (
    "You are an autonomous software engineering agent operating on a real GitHub repository.\n"
    "You receive a task, inspect the code with the provided tools, make minimal correct changes, "
    "and verify with lint/tests when available.\n"
    "Rules:\n"
    "- Prefer `edit_file` over `write_file` for small changes.\n"
    "- Always confirm an `old_string` is unique before editing.\n"
    "- Do not add comments unless asked. Match existing code style.\n"
    "- When finished, call `done` with a concise summary.\n"
    "- Keep tool output small and focused."
)


def _notify(notify: NotifyFn | None, msg: str) -> None:
    log.info("notify: %s", msg)
    if notify:
        try:
            notify(msg)
        except Exception as exc:  # noqa: BLE001
            log.warning("notify failed: %s", exc, exc_info=True)


def _run_loop(
    settings: Settings,
    gh: GitHubClient,
    system: str,
    task: str,
    ctx: ToolContext,
    notify: NotifyFn | None,
) -> str:
    client = make_client(settings)
    execute = make_executor(ctx)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": task},
    ]
    for i in range(1, settings.max_iterations + 1):
        msg = chat(client, settings.openrouter_model, messages, tools=TOOL_SCHEMAS)
        messages.append(msg.model_dump(exclude_none=True))  # type: ignore[arg-type]
        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            messages.append({
                "role": "user",
                "content": "If you are finished, call the `done` tool with a summary. Otherwise continue using the available tools.",
            })
            continue
        for idx, tc in enumerate(tool_calls):
            try:
                name = tc.function.name
            except Exception as exc:  # noqa: BLE001
                log.warning("malformed tool call: %s", exc)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": f"error: malformed tool call: {exc}"})
                continue
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to parse tool arguments for %s: %s", name, exc)
                args = {}
            _notify(notify, f"[{i}] tool: {name}")
            try:
                result = execute(name, args)
            except Exception as exc:  # noqa: BLE001
                result = f"error: {exc}"
            if not isinstance(result, str):
                result = json.dumps(result)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                }
            )
            if ctx.done:
                for remaining in tool_calls[idx + 1:]:
                    messages.append(
                        {"role": "tool", "tool_call_id": remaining.id, "content": "(skipped: agent called done)"}
                    )
                break
        if ctx.done:
            break
    if not ctx.done:
        ctx.summary = "(agent did not finish within iteration limit)"
    return ctx.summary


# ---------------------------------------------------------------------------
# Public tasks
# ---------------------------------------------------------------------------

def review_pr(
    settings: Settings,
    gh: GitHubClient,
    repo: str,
    pr_number: int,
    notify: NotifyFn | None = None,
) -> str:
    pr: PrInfo = gh.get_pr(repo, pr_number)
    _notify(notify, f"Reviewing PR #{pr_number} in {repo}: {pr.title}")

    diff_preview = pr.diff[:12000]
    task = (
        f"Review this pull request and post a clear, actionable review.\n"
        f"Repo: {repo}\nPR #{pr.number}: {pr.title}\nAuthor: {pr.author}\n"
        f"Base: {pr.base_ref} <- Head: {pr.head_ref}\n\n"
        f"Issue/body:\n{pr.body[:2000]}\n\n"
        f"Diff (truncated):\n```diff\n{diff_preview}\n```\n\n"
        "Inspect referenced files with `read_file`/`search` in the workspace to understand context.\n"
        "When done, call `done` with a markdown review covering: correctness, bugs, style, "
        "and concrete suggestions. Do not modify files."
    )

    # Clone base so review tools can read files.
    workspace = ""
    try:
        workspace = gh.clone(repo, pr.base_ref, f"review-{pr.number}-{int(time.time())}-{uuid.uuid4().hex[:6]}")
        ctx = ToolContext(workspace=workspace)
        review = _run_loop(settings, gh, BASE_SYSTEM, task, ctx, notify)
    finally:
        if workspace:
            gh.cleanup(workspace)

    if not ctx.done:
        _notify(notify, f"PR #{pr_number}: agent did not finish review.")
        gh.post_comment(repo, pr_number, f"Agent could not complete the review within the iteration limit.\n\n{review}")
        return review
    comment = f"### Agent review\n\n{review}"
    gh.post_comment(repo, pr_number, comment)
    _notify(notify, f"Posted review on PR #{pr_number}")
    return review


def fix_issue(
    settings: Settings,
    gh: GitHubClient,
    repo: str,
    issue_number: int,
    notify: NotifyFn | None = None,
) -> str:
    issue = gh.get_issue(repo, issue_number)
    title = issue.get("title", "")
    body = issue.get("body", "") or ""
    comments = gh.get_issue_comments(repo, issue_number)
    base_ref = _detect_default_branch(gh, repo)

    _notify(notify, f"Working on issue #{issue_number} in {repo}: {title}")

    thread = "\n\n".join(
        f"@{c.get('user', {}).get('login', '?')}: {c.get('body', '')}" for c in comments
    )
    branch = f"agent/issue-{issue_number}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    workspace = ""
    try:
        workspace = gh.clone(repo, base_ref, branch)
        ctx = ToolContext(workspace=workspace)
        task = (
            f"Resolve this GitHub issue by editing code in the workspace, then call `done`.\n"
            f"Repo: {repo}\nIssue #{issue_number}: {title}\n\n"
            f"Description:\n{body[:4000]}\n\n"
            f"Discussion:\n{thread[:3000]}\n\n"
            "Investigate the codebase, implement a minimal correct fix, and run lint/tests "
            "with `run_command` if a toolchain is present. Keep changes focused."
        )
        summary = _run_loop(settings, gh, BASE_SYSTEM, task, ctx, notify)
        if not ctx.done:
            _notify(notify, f"Issue #{issue_number}: agent did not finish, skipping commit/push.")
            gh.post_comment(
                repo, issue_number,
                f"Agent could not complete the fix within the iteration limit.\n\n{summary}",
            )
            return summary
        committed = gh.commit_all(workspace, f"Fix #{issue_number}: {title}\n\n{summary}")
        if not committed:
            _notify(notify, f"Issue #{issue_number}: no changes produced.")
            gh.post_comment(repo, issue_number, f"Agent analyzed the issue but made no code changes.\n\n{summary}")
            return summary
        try:
            gh.push(repo, workspace, branch)
            pr_url = gh.create_pr(
                repo,
                head=branch,
                base=base_ref,
                title=f"Fix #{issue_number}: {title}",
                body=f"Closes #{issue_number}\n\n{summary}",
            )
            _notify(notify, f"Issue #{issue_number} done. PR: {pr_url or '(no url)'}")
            if pr_url:
                gh.post_comment(repo, issue_number, f"Opened PR: {pr_url}")
        except Exception as exc:  # noqa: BLE001
            log.warning("push/create_pr failed for issue #%s: %s", issue_number, exc, exc_info=True)
            gh.post_comment(
                repo, issue_number,
                f"Agent committed changes but failed to push or open PR: {exc}\n\n{summary}",
            )
        return summary
    finally:
        if workspace:
            gh.cleanup(workspace)


def answer_issue(
    settings: Settings,
    gh: GitHubClient,
    repo: str,
    issue_number: int,
    notify: NotifyFn | None = None,
) -> str:
    issue = gh.get_issue(repo, issue_number)
    title = issue.get("title", "")
    body = issue.get("body", "") or ""
    comments = gh.get_issue_comments(repo, issue_number)
    _notify(notify, f"Answering issue #{issue_number} in {repo}: {title}")

    thread = "\n\n".join(
        f"@{c.get('user', {}).get('login', '?')}: {c.get('body', '')}" for c in comments
    )
    base_ref = _detect_default_branch(gh, repo)
    workspace = ""
    try:
        workspace = gh.clone(repo, base_ref, f"answer-{issue_number}-{int(time.time())}-{uuid.uuid4().hex[:6]}")
        ctx = ToolContext(workspace=workspace)
        task = (
            f"Answer this GitHub issue authoritatively by inspecting the code, then call `done`.\n"
            f"Repo: {repo}\nIssue #{issue_number}: {title}\n\n"
            f"Description:\n{body[:4000]}\n\nDiscussion:\n{thread[:3000]}\n\n"
            "Use `read_file`/`search` to ground your answer in the actual code. Do not modify files."
        )
        answer = _run_loop(settings, gh, BASE_SYSTEM, task, ctx, notify)
    finally:
        if workspace:
            gh.cleanup(workspace)

    if not ctx.done:
        _notify(notify, f"Issue #{issue_number}: agent did not finish, not posting reply.")
        gh.post_comment(repo, issue_number, f"Agent could not complete the answer within the iteration limit.\n\n{answer}")
        return answer
    gh.post_comment(repo, issue_number, f"### Agent reply\n\n{answer}")
    _notify(notify, f"Answered issue #{issue_number}")
    return answer


def _detect_default_branch(gh: GitHubClient, repo: str) -> str:
    try:
        r = gh.http.get(f"/repos/{repo}")
        if r.status_code == 200:
            return r.json().get("default_branch", "main")
        log.warning("failed to detect default branch for %s (status %s), falling back to 'main'", repo, r.status_code)
    except Exception as exc:  # noqa: BLE001
        log.warning("failed to detect default branch for %s: %s, falling back to 'main'", repo, exc)
    return "main"

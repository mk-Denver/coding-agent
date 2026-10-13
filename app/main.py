"""FastAPI composition root: health, GitHub webhooks, Telegram wiring."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

from . import agent
from .config import Settings, get_settings
from .github_client import GitHubClient
from .logger import get_logger
from .telegram_bot import build_bridge

log = get_logger("main")

# Module-level set to retain background tasks so they are not GC'd prematurely.
_bg_tasks: set[asyncio.Task] = set()


def _spawn(coro: Awaitable) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _bg_tasks.discard(t)
        if t.cancelled():
            return
        if t.exception() is not None:
            log.error("background task failed: %s", t.exception(), exc_info=t.exception())

    task.add_done_callback(_on_done)


async def _keepalive() -> None:
    """Ping our own health endpoint every 10 minutes to prevent Render free tier from sleeping."""
    url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8000") + "/health"
    while True:
        try:
            await asyncio.sleep(600)
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(url)
                log.debug("keepalive: %s -> %s", url, r.status_code)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("keepalive ping failed: %s", exc)


def _verify(body: bytes, signature: str | None, secret: str) -> bool:
    if not secret:
        return False
    if not signature:
        return False
    mac = hmac.new(secret.encode(), body, hashlib.sha256)
    expected = "sha256=" + mac.hexdigest()
    return hmac.compare_digest(expected, signature)


class AppContext:
    """Holds shared state wired at startup."""

    settings: Settings
    gh: GitHubClient
    notify: Callable[[str], None]
    bridge: Any  # TelegramBridge | None


ctx = AppContext()


def _async_dispatch(kind: str) -> Callable:
    async def dispatcher(repo: str, number: int, notify: Callable[[str], None]) -> None:
        fn = {
            "fix": agent.fix_issue,
            "review": agent.review_pr,
            "answer": agent.answer_issue,
        }[kind]
        await asyncio.to_thread(fn, ctx.settings, ctx.gh, repo, number, notify)

    return dispatcher


@asynccontextmanager
async def lifespan(_app: FastAPI):
    s = get_settings()
    if s.missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(s.missing)}")
    ctx.settings = s
    ctx.gh = GitHubClient(s)
    bridge = build_bridge(s)
    ctx.bridge = bridge

    if bridge is not None:
        bridge.register("fix", _async_dispatch("fix"))
        bridge.register("review", _async_dispatch("review"))
        bridge.register("answer", _async_dispatch("answer"))
        ctx.notify = bridge.notify_sync
        await bridge.start_polling()
    else:
        ctx.notify = lambda _msg: None

    log.info("Startup complete. valid=%s model=%s", s.is_valid, s.openrouter_model)
    keepalive = asyncio.create_task(_keepalive())
    yield
    keepalive.cancel()
    try:
        await asyncio.gather(keepalive, return_exceptions=True)
        for t in list(_bg_tasks):
            t.cancel()
        if _bg_tasks:
            await asyncio.gather(*_bg_tasks, return_exceptions=True)
            _bg_tasks.clear()
        if bridge is not None:
            await bridge.stop()
    finally:
        ctx.gh.close()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "config_valid": ctx.settings.is_valid,
        "model": ctx.settings.openrouter_model,
    }


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_github_event: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict[str, str]:
    body = await request.body()
    if not _verify(body, x_hub_signature_256, ctx.settings.github_webhook_secret):
        raise HTTPException(status_code=401, detail="invalid signature")

    if x_github_event == "ping":
        return {"status": "pong"}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json")

    event = x_github_event or ""
    repo = (
        payload.get("repository", {}).get("full_name")
        if payload.get("repository")
        else None
    )
    if not repo:
        return {"status": "ignored"}

    # Issue/PR comment mentioning @agent
    if event == "issue_comment" and payload.get("action") == "created":
        comment = payload.get("comment", {})
        author = comment.get("user", {}).get("login", "")
        if author == "coding-agent[bot]" or author == "coding-agent":
            return {"status": "self"}
        text = (comment.get("body") or "").lower()
        number = payload.get("issue", {}).get("number")
        is_pr = bool(payload.get("issue", {}).get("pull_request"))
        if "@agent" in text and number:
            if "review" in text and is_pr:
                _spawn(asyncio.to_thread(agent.review_pr, ctx.settings, ctx.gh, repo, number, ctx.notify))
            elif "answer" in text:
                _spawn(asyncio.to_thread(agent.answer_issue, ctx.settings, ctx.gh, repo, number, ctx.notify))
            elif "review" in text and not is_pr:
                return {"status": "ignored"}
            else:
                _spawn(asyncio.to_thread(agent.fix_issue, ctx.settings, ctx.gh, repo, number, ctx.notify))
            return {"status": "queued"}

    # New PR opened: auto-review if labeled agent:review
    if event == "pull_request" and payload.get("action") == "opened":
        labels = [l.get("name", "") for l in payload.get("pull_request", {}).get("labels", [])]
        if "agent:review" in labels:
            number = payload.get("pull_request", {}).get("number")
            if not number:
                return {"status": "ignored"}
            _spawn(asyncio.to_thread(agent.review_pr, ctx.settings, ctx.gh, repo, number, ctx.notify))
            return {"status": "queued"}

    # Issue labeled for the agent
    if event == "issues" and payload.get("action") == "labeled":
        label = payload.get("label", {}).get("name", "")
        number = payload.get("issue", {}).get("number")
        if label.lower() in {"agent", "agent:fix"} and number:
            _spawn(asyncio.to_thread(agent.fix_issue, ctx.settings, ctx.gh, repo, number, ctx.notify))
            return {"status": "queued"}

    return {"status": "ignored"}


@app.get("/")
async def root() -> dict[str, str]:
    return {"name": "coding-agent", "health": "/health", "webhook": "/webhook/github"}

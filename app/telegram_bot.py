"""Telegram bot bridge: outbound updates + inbound commands + NL intent routing."""
from __future__ import annotations

import asyncio
import json
import re
import threading
from typing import Any, Awaitable, Callable

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from .config import Settings
from .llm import chat, make_client
from .logger import get_logger

log = get_logger("telegram")

_REPO_RE = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
_GH_URL_RE = re.compile(
    r"https?://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/(?:pull|issues|discussions)/(\d+)"
)
_REPO_NUM_RE = re.compile(r"\b([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#(\d+)\b")
_ISSUE_NUM_RE = re.compile(r"\bissue(?:s)?\s*#?(\d+)\b", re.IGNORECASE)
_FIX_WORDS = ("fix", "implement", "patch", "repair", "resolve", "address", "close", "work on")
_ANSWER_WORDS = ("what", "why", "how", "explain", "question", "tell me", "understand", "investigate")

_INTENT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "route_intent",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["fix", "review", "answer"],
                    "description": "fix = implement a change for an issue; review = review a pull request; answer = inspect the repo and answer a question about an issue.",
                },
                "repo": {"type": "string", "description": "'owner/repo', or empty string if the message does not name one (a prior repo may be supplied separately)."},
                "number": {"type": "integer", "description": "Issue or PR number from the message. Use -1 if none is mentioned."},
            },
            "required": ["kind", "repo", "number"],
        },
    },
}

_INTENT_SYSTEM = (
    "You route a chat message to one of the agent's task types.\n"
    "Types: fix (make a code change for an issue), review (review a pull request), "
    "answer (look at the repo/code and reply about an issue).\n"
    "Prior repo from this conversation: {last_repo}.\n"
    "If the message omits the repository, reuse the prior repo (pass it in `repo`); "
    "if there is no prior repo either, return repo=''.\n"
    "Never invent or guess issue/PR numbers that are not in the message. "
    "Pull request usually means review; a why/how/what question about the code means answer; "
    "/request to change code means fix.\n"
    "This is a single tool call; always call route_intent."
)


def _fast_kind(text: str, is_pr_url: bool, is_issue_url: bool) -> str | None:
    t = text.lower()
    if "review" in t:
        return "review"
    if any(w in t for w in _FIX_WORDS):
        return "fix"
    if any(w in t for w in _ANSWER_WORDS):
        return "answer"
    if is_pr_url:
        return "review"
    if is_issue_url:
        return "answer"
    return None


def _match_intent_fast(text: str) -> tuple[str, str, int] | None:
    """Deterministic parse for the clearest messages; returns None to defer to the LLM."""
    is_pr_url = bool(re.search(r"https?://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/pull/", text, re.IGNORECASE)) or bool(
        re.search(r"\bpr\b", text, re.IGNORECASE) or re.search(r"pull request", text, re.IGNORECASE)
    )
    is_issue_url = bool(re.search(r"https?://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/issues/", text, re.IGNORECASE)) or bool(
        re.search(r"issues?", text, re.IGNORECASE)
    )
    kind = _fast_kind(text, is_pr_url, is_issue_url)
    if not kind:
        return None

    repo = ""
    number = -1
    gh_url = _GH_URL_RE.search(text)
    repo_num = _REPO_NUM_RE.search(text)
    if gh_url:
        owner_repo = "/".join(gh_url.group(0).split("github.com/", 1)[1].split("/")[:2])
        is_pr_url = "/pull/" in gh_url.group(0)
        if is_pr_url:
            if kind not in {"review", "fix"}:
                kind = "review"
        number = int(gh_url.group(1))
    elif repo_num:
        repo, number = repo_num.group(1), int(repo_num.group(2))
    if number == -1:
        issue_num = _ISSUE_NUM_RE.search(text)
        if issue_num:
            number = int(issue_num.group(1))
    if gh_url:
        repo = owner_repo
    if not repo or not _REPO_RE.fullmatch(repo) or number <= 0:
        return None
    return kind, repo, number

# Agent dispatch functions injected by main to avoid tight coupling.
# Signature: async def dispatcher(repo: str, number: int, notify: Callable[[str], None]) -> None
AgentDispatch = Callable[[str, int, Callable[[str], None]], Awaitable[None]]


class TelegramBridge:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.chat_id = settings.telegram_chat_id
        self.loop: asyncio.AbstractEventLoop | None = None
        self.app: Application = (
            ApplicationBuilder()
            .token(settings.telegram_bot_token)
            .post_init(self._post_init)
            .build()
        )
        self.dispatchers: dict[str, AgentDispatch] = {}
        self._tasks: set[asyncio.Task] = set()
        self.task_status: dict[str, dict] = {}
        self._status_lock = threading.Lock()
        self.last_repo: str = ""

    async def _post_init(self, _app: Application) -> None:
        self.loop = asyncio.get_running_loop()

    def register(self, name: str, fn: AgentDispatch) -> None:
        self.dispatchers[name] = fn

    def update_task_status(self, key: str, status: str) -> None:
        """Called from notify_sync to track live task progress."""
        if key not in self.task_status:
            self.task_status[key] = {"tools": 0, "last_tool": "", "status": status}
        else:
            self.task_status[key]["status"] = status

    def _track_tool(self, text: str) -> None:
        """Parse notify messages like '[3] tool: run_command' to update progress."""
        m = re.match(r"\[(\d+)\] tool: (\w+)", text)
        if not m:
            return
        with self._status_lock:
            for key, info in self.task_status.items():
                if info.get("status") not in ("done", "failed"):
                    info["tools"] = int(m.group(1))
                    info["last_tool"] = m.group(2)
                    break

    # ---- outbound (thread-safe) ---------------------------------------
    def notify_sync(self, text: str) -> None:
        if not self.loop or not self.chat_id:
            return
        if "[" in text and "] tool:" in text:
            self._track_tool(text)
        try:
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if self.loop is running:
                task = asyncio.ensure_future(
                    self.app.bot.send_message(chat_id=self.chat_id, text=text[:3500])
                )

                def _on_same_loop(f: asyncio.Future) -> None:
                    if f.exception() is not None:
                        log.warning("telegram notify failed: %s", f.exception())

                task.add_done_callback(_on_same_loop)
                return
            fut = asyncio.run_coroutine_threadsafe(
                self._send_with_log(text),
                self.loop,
            )

            def _on_done(f: asyncio.Future) -> None:
                if f.exception() is not None:
                    log.warning("telegram notify failed: %s", f.exception())

            fut.add_done_callback(_on_done)
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram notify failed: %s", exc)

    async def _send_with_log(self, text: str) -> None:
        """Wrapper that logs send failures with context."""
        try:
            await self.app.bot.send_message(chat_id=self.chat_id, text=text[:3500])
            log.debug("telegram message sent: %s", text[:80])
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram send_message failed: %s", exc)
            raise

    # ---- lifecycle -----------------------------------------------------
    async def start_polling(self) -> None:
        await self.app.initialize()
        await self.app.start()
        for attempt in range(5):
            try:
                await self.app.updater.start_polling(drop_pending_updates=True)
                log.info("Telegram polling started (chat=%s)", self.chat_id)
                return
            except Exception as exc:  # noqa: BLE001
                if "Conflict" in str(exc) and attempt < 4:
                    wait = (attempt + 1) * 5
                    log.warning("Telegram conflict on startup, retrying in %ds (attempt %d/5)...", wait, attempt + 2)
                    await asyncio.sleep(wait)
                    continue
                raise

    async def stop(self) -> None:
        try:
            for t in list(self._tasks):
                t.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
                self._tasks.clear()
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
        except Exception as exc:  # noqa: BLE001
            log.warning("Telegram stop failed: %s", exc)

    def install_handlers(self) -> None:
        self.app.add_handler(CommandHandler("start", self._h_start))
        self.app.add_handler(CommandHandler("help", self._h_help))
        self.app.add_handler(CommandHandler("status", self._h_status))
        self.app.add_handler(CommandHandler("tasks", self._h_tasks))
        self.app.add_handler(CommandHandler("fix", self._h_dispatch("fix")))
        self.app.add_handler(CommandHandler("review", self._h_dispatch("review")))
        self.app.add_handler(CommandHandler("answer", self._h_dispatch("answer")))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._h_message))

    # ---- authorization --------------------------------------------------
    def _is_authorized(self, update: Update) -> bool:
        chat_id = str(update.effective_chat.id) if update.effective_chat else ""
        if chat_id != self.chat_id:
            log.warning("unauthorized chat_id=%s (expected %s)", chat_id, self.chat_id)
            return False
        return True

    # ---- handlers ------------------------------------------------------
    async def _h_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update) or update.effective_message is None:
            return
        await update.effective_message.reply_text(self._help_text())

    async def _h_help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update) or update.effective_message is None:
            return
        await update.effective_message.reply_text(self._help_text())

    async def _h_status(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update) or update.effective_message is None:
            return
        s = self.settings
        await update.effective_message.reply_text(
            f"Agent online.\nmodel: {s.openrouter_model}\n"
            f"github: {'ok' if s.github_token else 'MISSING'}\n"
            f"openrouter: {'ok' if s.openrouter_api_key else 'MISSING'}"
        )

    async def _h_tasks(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update) or update.effective_message is None:
            return
        if not self.task_status:
            await update.effective_message.reply_text("No active or recent tasks.")
            return
        lines: list[str] = []
        with self._status_lock:
            snapshot = list(self.task_status.items())
        for key, info in snapshot:
            status_icon = {"done": "✅", "failed": "❌", "cancelled": "❌", "working": "🔄"}.get(info["status"], "🔄")
            lines.append(f"{status_icon} {key}\n   tools run: {info['tools']}, last: {info['last_tool']}")
        await update.effective_message.reply_text("\n".join(lines))

    def _h_dispatch(self, kind: str):
        async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._is_authorized(update) or update.effective_message is None:
                return
            args = ctx.args
            if len(args) < 2 or not args[1].isdigit():
                await update.effective_message.reply_text(
                    f"Usage: /{kind} <owner/repo> <number>"
                )
                return
            repo = args[0]
            if not _REPO_RE.fullmatch(repo):
                await update.effective_message.reply_text(f"Invalid repo format: {repo}")
                return
            number = int(args[1])
            await update.effective_message.reply_text(self._start_task(kind, repo, number))
        return handler

    def _start_task(self, kind: str, repo: str, number: int) -> str:
        if kind not in self.dispatchers:
            return f"no dispatcher for {kind}"
        task_key = f"{kind} {repo}#{number}"
        with self._status_lock:
            self.task_status[task_key] = {"tools": 0, "last_tool": "", "status": "working"}
        self.last_repo = repo
        fn = self.dispatchers[kind]
        task = asyncio.create_task(fn(repo, number, self.notify_sync))
        self._tasks.add(task)

        def _on_done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            with self._status_lock:
                entry = self.task_status.get(task_key)
            if entry is not None and not t.cancelled() and t.exception() is not None:
                log.error("dispatcher %s failed for %s#%s: %s", kind, repo, number, t.exception(), exc_info=t.exception())
                self.notify_sync(f"/{kind} {repo}#{number} failed: {t.exception()}")
            with self._status_lock:
                entry = self.task_status.get(task_key)
                if entry is None:
                    return
                if t.cancelled():
                    entry["status"] = "cancelled"
                    return
                if t.exception() is not None:
                    entry["status"] = "failed"
                else:
                    entry["status"] = "done"

        task.add_done_callback(_on_done)
        return f"Queued {kind} on {repo}#{number}. I'll send updates here."

    # ---- natural language routing --------------------------------------
    def _parse_intent(self, text: str) -> tuple[str, str, int] | None:
        fast = _match_intent_fast(text)
        if fast:
            log.debug("intent fast path: %s", fast)
            return fast
        return self._llm_intent(text)

    def _llm_intent(self, text: str) -> tuple[str, str, int] | None:
        try:
            client = make_client(self.settings)
            model = self.settings.intent_model or self.settings.openrouter_model
            messages = [
                {"role": "system", "content": _INTENT_SYSTEM.format(last_repo=self.last_repo or "(none)")},
                {"role": "user", "content": text},
            ]
            msg = chat(client, model, messages, tools=[_INTENT_TOOL], temperature=0)
        except Exception as exc:  # noqa: BLE001
            log.warning("intent parse failed: %s", exc)
            return None
        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            return None
        try:
            args = json.loads(tool_calls[0].function.arguments or "{}")
        except Exception:
            log.warning("intent tool call returned invalid JSON")
            return None
        kind = str(args.get("kind", "")).lower()
        repo = str(args.get("repo", "") or "").strip()
        try:
            number = int(args.get("number", -1))
        except (TypeError, ValueError):
            number = -1
        if kind not in {"fix", "review", "answer"}:
            return None
        if repo == "(none)":
            repo = ""
        if not repo and self.last_repo:
            repo = self.last_repo
        if repo == "(none)" or not _REPO_RE.fullmatch(repo) or number <= 0:
            return None
        return kind, repo, number

    async def _h_message(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update) or update.effective_message is None:
            return
        text = (update.effective_message.text or "").strip()
        if not text:
            return
        intent = await asyncio.to_thread(self._parse_intent, text)
        if intent is None:
            await update.effective_message.reply_text(
                "I couldn't work out a task from that message.\n"
                "Use /fix <owner/repo> <issue#>, /review <owner/repo> <pr#>, /answer <owner/repo> <issue#>, "
                "or tell me something like \u201cfix issue 3 in owner/repo\u201d or paste a GitHub link."
            )
            return
        kind, repo, number = intent
        await update.effective_message.reply_text(self._start_task(kind, repo, number))

    def _help_text(self) -> str:
        return (
            "Coding agent commands:\n"
            "/fix <owner/repo> <issue#> - implement the fix and open a PR\n"
            "/review <owner/repo> <pr#> - review a pull request\n"
            "/answer <owner/repo> <issue#> - answer an issue\n"
            "/status - show config\n"
            "/tasks - show active and recent task progress\n\n"
            "You can also talk to me naturally: \u201cfix issue 5 in owner/repo\u201d, "
            "\u201creview https://github.com/owner/repo/pull/9\u201d, or \u201cwhy is issue 3 failing?\u201d "
            "(I remember the last repo for short follow-ups)."
        )


def build_bridge(settings: Settings) -> TelegramBridge | None:
    if not settings.telegram_bot_token:
        log.warning("TELEGRAM_BOT_TOKEN not set; Telegram disabled.")
        return None
    if not settings.telegram_chat_id:
        log.warning("TELEGRAM_CHAT_ID not set; Telegram disabled.")
        return None
    bridge = TelegramBridge(settings)
    bridge.install_handlers()
    return bridge

"""Telegram bot bridge: outbound updates + inbound commands."""
from __future__ import annotations

import asyncio
import re
import threading
from typing import Awaitable, Callable

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

from .config import Settings
from .logger import get_logger

log = get_logger("telegram")

_REPO_RE = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")

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
        for key, info in self.task_status.items():
            status_icon = {"done": "✅", "failed": "❌", "working": "🔄"}.get(info["status"], "🔄")
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
            if kind not in self.dispatchers:
                await update.effective_message.reply_text(f"no dispatcher for {kind}")
                return
            await update.effective_message.reply_text(
                f"Queued {kind} on {repo}#{number}. I'll send updates here."
            )
            task_key = f"{kind} {repo}#{number}"
            self.task_status[task_key] = {"tools": 0, "last_tool": "", "status": "working"}
            fn = self.dispatchers[kind]
            task = asyncio.create_task(fn(repo, number, self.notify_sync))
            self._tasks.add(task)

            def _on_done(t: asyncio.Task) -> None:
                self._tasks.discard(t)
                with self._status_lock:
                    entry = self.task_status.get(task_key)
                    if entry is None:
                        return
                    if t.cancelled():
                        entry["status"] = "cancelled"
                        return
                    if t.exception() is not None:
                        entry["status"] = "failed"
                        log.error("dispatcher %s failed for %s#%s: %s", kind, repo, number, t.exception())
                    else:
                        entry["status"] = "done"

            task.add_done_callback(_on_done)
        return handler

    def _help_text(self) -> str:
        return (
            "Coding agent commands:\n"
            "/fix <owner/repo> <issue#> - implement the fix and open a PR\n"
            "/review <owner/repo> <pr#> - review a pull request\n"
            "/answer <owner/repo> <issue#> - answer an issue\n"
            "/status - show config\n"
            "/tasks - show active and recent task progress\n\n"
            "Tip: reply here to steer the agent between tasks."
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

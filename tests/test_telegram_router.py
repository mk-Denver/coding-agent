import asyncio
import json
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletionMessage, ChatCompletionMessageToolCall

import app.telegram_bot as tb_mod
from app.config import Settings
from app.telegram_bot import TelegramBridge, _match_intent_fast


def make_settings(**kw) -> Settings:
    return Settings(
        openrouter_api_key="k",
        openrouter_model="router-default-model",
        github_token="t",
        github_webhook_secret="s",
        telegram_bot_token="b",
        telegram_chat_id="42",
        **kw,
    )


def make_bridge(**kw) -> TelegramBridge:
    return TelegramBridge(make_settings(**kw))


def intent_tool_call(args: dict) -> ChatCompletionMessageToolCall:
    return ChatCompletionMessageToolCall(
        id="1",
        type="function",
        function={"name": "route_intent", "arguments": json.dumps(args)},
    )


class FakeLLM:
    def __init__(self, scripted: list[ChatCompletionMessage]):
        self.scripted = list(scripted)
        chat_ns = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.chat = chat_ns

    def _create(self, **kwargs):
        msg = self.scripted.pop(0) if self.scripted else ChatCompletionMessage(
            role="assistant", content=None, tool_calls=None
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def patch_llm(monkeypatch, scripted):
    fake = FakeLLM(scripted)
    monkeypatch.setattr(tb_mod, "make_client", lambda settings: fake)
    return fake


def test_fast_path_pr_link_review():
    assert _match_intent_fast("please review https://github.com/o/r/pull/31") == ("review", "o/r", 31)


def test_fast_path_fix_word_with_issue_link():
    assert _match_intent_fast("fix https://github.com/o/r/issues/7 asap") == ("fix", "o/r", 7)


def test_fast_path_repo_hash():
    assert _match_intent_fast("review o/r#4") == ("review", "o/r", 4)


def test_fast_path_answer_by_default_on_issue():
    assert _match_intent_fast("https://github.com/o/r/issues/11") == ("answer", "o/r", 11)


def test_fast_path_fix_beats_pr_default():
    assert _match_intent_fast("fix this pr o/r#8") == ("fix", "o/r", 8)


def test_fast_path_none_without_repo():
    assert _match_intent_fast("why is issue 4 failing?") is None


def test_fast_path_ignores_non_url_when_no_repo():
    assert _match_intent_fast("hey how are you doing today") is None


def test_llm_path_uses_last_repo_memory(monkeypatch):
    bridge = make_bridge()
    bridge.last_repo = "o/r"
    patch_llm(monkeypatch, [
        ChatCompletionMessage(role="assistant", content=None, tool_calls=[
            intent_tool_call({"kind": "answer", "repo": "", "number": 4}),
        ]),
    ])
    assert bridge._parse_intent("why is issue 4 failing?") == ("answer", "o/r", 4)


def test_llm_path_message_provided_repo_wins_over_memory(monkeypatch):
    bridge = make_bridge()
    bridge.last_repo = "old/repo"
    patch_llm(monkeypatch, [
        ChatCompletionMessage(role="assistant", content=None, tool_calls=[
            intent_tool_call({"kind": "fix", "repo": "new/repo", "number": 2}),
        ]),
    ])
    assert bridge._parse_intent("fix issue 2 in new/repo") == ("fix", "new/repo", 2)
    assert bridge.last_repo == "old/repo"


def test_llm_path_missing_number_yields_none(monkeypatch):
    bridge = make_bridge()
    bridge.last_repo = "o/r"
    patch_llm(monkeypatch, [
        ChatCompletionMessage(role="assistant", content=None, tool_calls=[
            intent_tool_call({"kind": "fix", "repo": "o/r", "number": -1}),
        ]),
    ])
    assert bridge._parse_intent("go fix something") is None


def test_llm_path_bad_kind_yields_none(monkeypatch):
    bridge = make_bridge()
    patch_llm(monkeypatch, [
        ChatCompletionMessage(role="assistant", content=None, tool_calls=[
            intent_tool_call({"kind": "delete", "repo": "o/r", "number": 3}),
        ]),
    ])
    assert bridge._parse_intent("delete o/r#3") is None


def test_llm_path_llm_error_yields_none(monkeypatch):
    bridge = make_bridge()

    def boom(settings):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(tb_mod, "make_client", boom)
    assert bridge._parse_intent("do the thing") is None


def test_intent_model_env_takes_priority(monkeypatch):
    bridge = make_bridge(intent_model="cheap-fast-model")
    seen = {}

    def fake_chat(client, model, messages, tools=None, temperature=0.2):
        seen["model"] = model
        return ChatCompletionMessage(role="assistant", content=None, tool_calls=[
            intent_tool_call({"kind": "answer", "repo": "o/r", "number": 1}),
        ])

    monkeypatch.setattr(tb_mod, "chat", fake_chat)
    result = bridge._llm_intent("what about o/r#1")
    assert result == ("answer", "o/r", 1)
    assert seen["model"] == "cheap-fast-model"


def test_intent_model_defaults_to_openrouter_model(monkeypatch):
    bridge = make_bridge()
    seen = {}

    def fake_chat(client, model, messages, tools=None, temperature=0.2):
        seen["model"] = model
        raise RuntimeError("stop")

    monkeypatch.setattr(tb_mod, "chat", fake_chat)
    bridge._llm_intent("anything")
    assert seen["model"] == "router-default-model"


def test_start_task_registers_and_tracks():
    bridge = make_bridge()
    started = {}

    async def fake_dispatch(repo, number, notify):
        started["args"] = (repo, number)

    bridge.register("fix", fake_dispatch)

    async def scenario():
        reply = bridge._start_task("fix", "o/r", 9)
        await asyncio.sleep(0.1)
        return reply

    reply = asyncio.run(scenario())
    assert "Queued fix on o/r#9" in reply
    assert started["args"] == ("o/r", 9)
    assert bridge.last_repo == "o/r"
    entry = bridge.task_status["fix o/r#9"]
    assert entry["status"] == "done"


def test_start_task_failure_marks_failed():
    bridge = make_bridge()

    async def boom_dispatch(repo, number, notify):
        raise RuntimeError("clone exploded")

    bridge.register("fix", boom_dispatch)

    async def scenario():
        reply = bridge._start_task("fix", "o/r", 9)
        await asyncio.sleep(0.2)
        return reply

    asyncio.run(scenario())
    assert bridge.task_status["fix o/r#9"]["status"] == "failed"


def test_start_task_unknown_kind():
    bridge = make_bridge()
    assert bridge._start_task("nonsense", "o/r", 1) == "no dispatcher for nonsense"


def test_parse_message_end_to_end(monkeypatch):
    bridge = make_bridge()
    bridge.last_repo = "o/r"
    patch_llm(monkeypatch, [
        ChatCompletionMessage(role="assistant", content=None, tool_calls=[
            intent_tool_call({"kind": "review", "repo": "o/r", "number": 6}),
        ]),
    ])
    result = bridge._parse_intent("can you look at pr 6?")
    assert result == ("review", "o/r", 6)

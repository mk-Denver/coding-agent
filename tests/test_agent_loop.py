import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from openai.types.chat import (
    ChatCompletionMessageToolCall,
    ChatCompletionMessage,
)

import app.agent as agent_mod
from app.config import Settings
from app.tools import ToolContext


def tc(call_id: str, name: str, args: dict) -> ChatCompletionMessageToolCall:
    return ChatCompletionMessageToolCall(
        id=call_id,
        type="function",
        function={"name": name, "arguments": json.dumps(args)},
    )


def tc_malformed(call_id: str, name: str) -> ChatCompletionMessageToolCall:
    return ChatCompletionMessageToolCall(
        id=call_id,
        type="function",
        function={"name": name, "arguments": "{not valid json"},
    )


class FakeLLM:
    def __init__(self, scripted: list[ChatCompletionMessage]):
        self.scripted = list(scripted)
        self.seen_messages: list = []
        chat_ns = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.chat = chat_ns

    def _create(self, **kwargs):
        self.seen_messages.append(list(kwargs.get("messages", [])))
        msg = self.scripted.pop(0) if self.scripted else ChatCompletionMessage(
            role="assistant", content=None, tool_calls=None
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


@pytest.fixture
def workspace(tmp_path: Path) -> str:
    (tmp_path / "hello.txt").write_text("hi\n", encoding="utf-8")
    return str(tmp_path)


def make_settings(max_iterations: int = 10) -> Settings:
    return Settings(
        openrouter_api_key="k",
        openrouter_model="m",
        github_token="t",
        github_webhook_secret="s",
        telegram_bot_token="b",
        telegram_chat_id="1",
        max_iterations=max_iterations,
    )


def run(monkeypatch, scripted, workspace, max_iterations=10):
    fake = FakeLLM(scripted)
    monkeypatch.setattr(agent_mod, "make_client", lambda settings: fake)
    ctx = ToolContext(workspace=workspace)
    summary = agent_mod._run_loop(
        make_settings(max_iterations), None, "system", "task", ctx, None
    )
    return ctx, summary, fake


def test_tool_call_write_then_done(monkeypatch, workspace):
    scripted = [
        ChatCompletionMessage(role="assistant", content=None, tool_calls=[
            tc("1", "write_file", {"path": "out.txt", "content": "body"}),
        ]),
        ChatCompletionMessage(role="assistant", content=None, tool_calls=[
            tc("2", "done", {"summary": "wrote it"}),
        ]),
    ]
    ctx, summary, fake = run(monkeypatch, scripted, workspace)
    assert ctx.done is True
    assert summary == "wrote it"
    assert (Path(workspace) / "out.txt").read_text(encoding="utf-8") == "body"
    assert len(fake.seen_messages) == 2
    final_round = fake.seen_messages[-1]
    assert final_round[-1]["role"] == "tool"
    assert final_round[-1]["tool_call_id"] == "1"


def test_malformed_tool_arguments_get_error_reply(monkeypatch, workspace):
    scripted = [
        ChatCompletionMessage(role="assistant", content=None, tool_calls=[
            tc_malformed("1", "write_file"),
        ]),
        ChatCompletionMessage(role="assistant", content=None, tool_calls=[
            tc("2", "done", {"summary": "recovered"}),
        ]),
    ]
    ctx, summary, fake = run(monkeypatch, scripted, workspace)
    assert ctx.done is True
    assert summary == "recovered"
    error_round = fake.seen_messages[1]
    err_msg = [m for m in error_round if m.get("role") == "tool"][0]
    assert "invalid tool arguments JSON" in err_msg["content"]
    assert not (Path(workspace) / "write_file.txt").exists()


def test_plain_text_fallback_used_as_summary(monkeypatch, workspace):
    scripted = [
        ChatCompletionMessage(role="assistant", content="Here is my final answer.", tool_calls=None),
    ]
    ctx, summary, _ = run(monkeypatch, scripted, workspace, max_iterations=3)
    assert ctx.done is False
    assert summary == "Here is my final answer."


def test_iteration_limit_without_content(monkeypatch, workspace):
    scripted = []
    ctx, summary, _ = run(monkeypatch, scripted, workspace, max_iterations=2)
    assert ctx.done is False
    assert summary == "(agent did not finish within iteration limit)"


def test_continue_nudge_appended_when_no_tool_calls(monkeypatch, workspace):
    scripted = [
        ChatCompletionMessage(role="assistant", content=None, tool_calls=None),
        ChatCompletionMessage(role="assistant", content=None, tool_calls=[
            tc("1", "done", {"summary": "s"}),
        ]),
    ]
    _, summary, fake = run(monkeypatch, scripted, workspace)
    assert summary == "s"
    assert any(
        m["role"] == "user" and "call the `done` tool" in m["content"]
        for m in fake.seen_messages[-1]
    )


def test_review_pr_flow_posts_review(monkeypatch, tmp_path):
    calls = {}
    from app.github_client import GitHubClient, PrInfo

    from app.config import Settings

    class FakeGh:
        def __init__(self):
            self.ws = str(tmp_path / "ws")
            Path(self.ws).mkdir()

        def get_pr(self, repo, number):
            return PrInfo(
                number=number, title="T", body="", head_ref="h", base_ref="b",
                diff="diff text", author="me", head_sha="abc123",
            )

        def clone(self, repo, ref, branch):
            calls["cloned"] = (repo, ref, branch)
            return self.ws

        def cleanup(self, ws):
            calls["cleaned"] = ws

        def post_review(self, repo, number, body, head_sha, inline_comments=None):
            calls["review"] = (repo, number, body, head_sha, inline_comments)
            return "http://review/url"

        def post_comment(self, repo, number, body):
            raise AssertionError("comment is wrong outcome here")

    scripted = [
        ChatCompletionMessage(role="assistant", content=None, tool_calls=[
            tc("1", "done", {"summary": "looks good"}),
        ]),
    ]
    FakeGh_instance = FakeGh()
    monkeypatch.setattr(agent_mod, "make_client", lambda settings: FakeLLM(scripted))
    s = make_settings()
    review = agent_mod.review_pr(s, FakeGh_instance, "o/r", 5)
    assert review == "looks good"
    assert calls["cloned"][0] == "o/r"
    assert calls["cleaned"] == FakeGh_instance.ws
    repo, number, body, head_sha, inline = calls["review"]
    assert head_sha == "abc123"
    assert "### Agent review" in body


def test_fix_issue_no_changes_leaves_comment(monkeypatch, tmp_path):
    from app.config import Settings

    class FakeGh:
        def __init__(self):
            self.ws = str(tmp_path / "ws")
            Path(self.ws).mkdir()

        def get_issue(self, repo, number):
            return {"title": "t", "body": "b"}

        def get_issue_comments(self, repo, number):
            return []

        def clone(self, repo, ref, branch):
            return self.ws

        def cleanup(self, ws):
            pass

        def commit_all(self, ws, msg):
            return False

        def post_comment(self, repo, number, body):
            posted.append(body)

    posted = []

    scripted = [
        ChatCompletionMessage(role="assistant", content=None, tool_calls=[
            tc("1", "done", {"summary": "nothing to change"}),
        ]),
    ]
    monkeypatch.setattr(agent_mod, "make_client", lambda settings: FakeLLM(scripted))
    monkeypatch.setattr(agent_mod, "_detect_default_branch", lambda gh, repo: "main")
    summary = agent_mod.fix_issue(make_settings(), FakeGh(), "o/r", 11)
    assert summary == "nothing to change"
    assert any("no code changes" in p for p in posted)

from pathlib import Path
from types import SimpleNamespace

import pytest

import app.github_client as gh_mod
from app.config import Settings


def make_gh() -> gh_mod.GitHubClient:
    s = Settings(
        openrouter_api_key="k",
        github_token="gh_test_token",
        github_webhook_secret="s",
        telegram_bot_token="b",
        telegram_chat_id="1",
    )
    return gh_mod.GitHubClient(s)


def test_parse_repo():
    gh = make_gh()
    assert gh.parse_repo("owner/repo") == ("owner", "repo")
    assert gh.parse_repo("o/r/sub") == ("o", "r/sub")
    with pytest.raises(ValueError):
        gh.parse_repo("justname")
    with pytest.raises(ValueError):
        gh.parse_repo("owner/")
    with pytest.raises(ValueError):
        gh.parse_repo("")


def test_repo_url():
    gh = make_gh()
    assert gh.repo_url("o/r") == "https://github.com/o/r.git"


def test_redact_hides_token():
    gh = make_gh()
    text = "failed url https://x:gh_test_token@github.com/o/r.git owner gh_test_token"
    out = gh._redact(text)
    assert "gh_test_token" not in out
    assert "https://***@" in out


def test_make_git_env_builds_per_op_helper_and_cleans_up():
    gh = make_gh()
    env, askpass = gh._make_git_env()
    try:
        assert env["GIT_ASKPASS"] == askpass
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        p = Path(askpass)
        assert p.exists()
        assert p.suffix == ".sh"
        content = p.read_text(encoding="utf-8")
        assert "gh_test_token" in content
        assert content.startswith("#!/bin/sh")
    finally:
        gh._rm(askpass)
    assert not Path(askpass).exists()


def test_mirror_two_concurrent_git_ops_do_not_share_askpass():
    gh = make_gh()
    env1, a1 = gh._make_git_env()
    env2, a2 = gh._make_git_env()
    assert a1 != a2
    gh._rm(a1)
    assert not Path(a1).exists()
    # second op unaffected
    assert Path(a2).exists()
    content = Path(a2).read_text(encoding="utf-8")
    assert "gh_test_token" in content
    gh._rm(a2)


def test_add_git_excludes_writes_agent_tmp(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".git" / "info").mkdir(parents=True)
    (ws / ".git" / "info" / "exclude").write_text("# existing\n", encoding="utf-8")
    gh_mod.GitHubClient._add_git_excludes(str(ws))
    content = (ws / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "# existing" in content
    assert ".agent-tmp/" in content


def test_add_git_excludes_missing_info_dir_is_tolerated(tmp_path):
    ws = tmp_path / "nows"
    gh_mod.GitHubClient._add_git_excludes(str(ws))  # must not raise


class FakeResponse:
    def __init__(self, data, status_code=201):
        self._data = data
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


def test_post_review_omits_commit_id_when_sha_empty():
    gh = make_gh()
    calls = []

    def fake_post(path, json=None):
        calls.append((path, json))
        return FakeResponse({"html_url": "http://u"})

    gh.http = SimpleNamespace(post=fake_post)
    url = gh.post_review("o/r", 5, "body", "", inline_comments=[
        {"path": "a.py", "line": 3, "body": "note"},
    ])
    assert url == "http://u"
    path, payload = calls[0]
    assert path == "/repos/o/r/pulls/5/reviews"
    assert "commit_id" not in payload
    assert payload["comments"][0]["side"] == "RIGHT"
    assert payload["event"] == "COMMENT"


def test_post_review_includes_commit_id_when_present():
    gh = make_gh()
    calls = []

    def fake_post(path, json=None):
        calls.append((path, json))
        return FakeResponse({"html_url": ""})

    gh.http = SimpleNamespace(post=fake_post)
    gh.post_review("o/r", 5, "body", "deadbeef")
    path, payload = calls[0]
    assert payload["commit_id"] == "deadbeef"
    assert "comments" not in payload


def test_post_comment_url_and_payload():
    gh = make_gh()
    calls = []

    def fake_post(path, json=None):
        calls.append((path, json))
        return FakeResponse({"id": 1}, 201)

    gh.http = SimpleNamespace(post=fake_post)
    gh.post_comment("o/r", 9, "hello")
    path, payload = calls[0]
    assert path == "/repos/o/r/issues/9/comments"
    assert payload == {"body": "hello"}

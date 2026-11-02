"""Regression tests for GitHubClient.get_file.

GitHub omits `content` (returns null) for files larger than ~1 MB and sends an
empty `content` for empty files. Both must not crash the client.
"""
from types import SimpleNamespace

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


class Resp:
    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text
        self.status_code = 200

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")

    def json(self):
        return self._payload


def test_get_file_normal_base64():
    gh = make_gh()
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return Resp(payload={"type": "file", "encoding": "base64",
                             "content": "aGVsbG8=", "size": 5})

    gh.http = SimpleNamespace(get=fake_get)
    assert gh.get_file("o/r", "file.txt") == "hello"
    # normal path does not need the raw download URL
    assert len(calls) == 1


def test_get_file_large_falls_back_to_download_url():
    gh = make_gh()
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if url.startswith("/repos/"):
            return Resp(payload={"type": "file", "encoding": "base64",
                                 "content": None, "size": 2_000_000,
                                 "download_url": "https://raw.githubusercontent.com/o/r/main/big.bin"})
        return Resp(text="BIGCONTENT")

    gh.http = SimpleNamespace(get=fake_get)
    assert gh.get_file("o/r", "big.bin") == "BIGCONTENT"
    assert "https://raw.githubusercontent.com/o/r/main/big.bin" in calls


def test_get_file_empty_returns_empty():
    gh = make_gh()

    def fake_get(url, **kwargs):
        # GitHub returns content="" for empty files and still sets download_url.
        if url.startswith("/repos/"):
            return Resp(payload={"type": "file", "encoding": "base64",
                                 "content": "", "size": 0,
                                 "download_url": "https://raw.githubusercontent.com/o/r/main/empty.txt"})
        return Resp(text="")

    gh.http = SimpleNamespace(get=fake_get)
    assert gh.get_file("o/r", "empty.txt") == ""


def test_get_file_null_without_download_url_returns_empty():
    gh = make_gh()

    def fake_get(url, **kwargs):
        # defensive: content null and no download_url -> empty, not a crash.
        return Resp(payload={"type": "file", "encoding": "base64",
                             "content": None, "size": 0})

    gh.http = SimpleNamespace(get=fake_get)
    assert gh.get_file("o/r", "x.txt") == ""
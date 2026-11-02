"""Unittest-runnable regression tests for the two fixes.

These use only the standard-library ``unittest``/``mock`` (no pytest
fixtures), so they are collected and run by ``python -m unittest discover``,
which is the host verifier's configured runner.

They exercise the same code paths as the pytest suites in
``test_webfetch.py`` and ``test_github_client.py``:

* ``web_fetch`` must reject redirect chains that exceed ``_MAX_FETCH_HOPS``
  and self-referential/cyclic redirect loops (instead of returning the empty
  redirect body as "content").
* ``GitHubClient.get_file`` must tolerate ``content: null`` (GitHub omits it
  for files > ~1 MB) and empty ``content`` (empty files) without raising.
"""
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import app.github_client as gh_mod
import app.tools as tools_mod
from app.config import Settings
from app.tools import ToolContext, make_executor


class _FakeResponse:
    def __init__(self, status_code, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text

    @property
    def is_redirect(self):
        return (
            self.status_code in {301, 302, 303, 307, 308}
            and "location" in self.headers
        )

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")


class _FakeClient:
    def __init__(self, *args, **kwargs):
        self.requested: list[str] = []
        self.handler = lambda url: _FakeResponse(200, {}, "")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, **kwargs):
        self.requested.append(url)
        return self.handler(url)


class _Resp:
    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text
        self.status_code = 200

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")

    def json(self):
        return self._payload


class WebFetchRedirectTests(unittest.TestCase):
    """web_fetch must surface an error for runaway / cyclic redirects."""

    def _run(self, handler, url="http://example.com/start"):
        client = _FakeClient()
        client.handler = handler
        with tempfile.TemporaryDirectory() as workspace:
            ctx = ToolContext(workspace=workspace)
            execute = make_executor(ctx)
            with mock.patch.object(tools_mod.httpx, "Client", return_value=client), \
                    mock.patch.object(
                        tools_mod.socket,
                        "getaddrinfo",
                        lambda h, p: [(None, None, None, "", ("93.184.216.34", 0))],
                    ):
                return execute("web_fetch", {"url": url}), client

    def test_too_many_redirects(self):
        counter = {"n": 0}

        def handler(url):
            n = counter["n"]
            counter["n"] += 1
            return _FakeResponse(302, {"location": f"/hop{n}"})

        out, client = self._run(handler)
        self.assertIn("too many redirects", out)
        self.assertEqual(len(client.requested), tools_mod._MAX_FETCH_HOPS)
        self.assertTrue(
            all(u.startswith("http://example.com/") for u in client.requested)
        )

    def test_self_redirect_loop(self):
        def handler(url):
            if url.endswith("/start"):
                return _FakeResponse(302, {"location": "/start"})
            return _FakeResponse(200, {}, "landing")

        out, client = self._run(handler)
        self.assertIn("redirect loop detected", out)
        self.assertEqual(client.requested, ["http://example.com/start"])

    def test_cyclic_redirect_loop(self):
        def handler(url):
            if url.endswith("/r1"):
                return _FakeResponse(302, {"location": "/r2"})
            if url.endswith("/r2"):
                return _FakeResponse(302, {"location": "/r1"})
            return _FakeResponse(200, {}, "landing")

        out, client = self._run(handler, url="http://example.com/r1")
        self.assertIn("redirect loop detected", out)
        self.assertEqual(
            client.requested, ["http://example.com/r1", "http://example.com/r2"]
        )

    def test_safe_redirect_chain_still_followed(self):
        def handler(url):
            if url.endswith("/r1"):
                return _FakeResponse(302, {"location": "/r2"})
            return _FakeResponse(200, {}, "landing")

        out, client = self._run(handler, url="http://example.com/r1")
        self.assertEqual(out, "landing")
        self.assertEqual(
            client.requested, ["http://example.com/r1", "http://example.com/r2"]
        )


def _make_gh() -> gh_mod.GitHubClient:
    return gh_mod.GitHubClient(
        Settings(
            openrouter_api_key="k",
            github_token="gh_test_token",
            github_webhook_secret="s",
            telegram_bot_token="b",
            telegram_chat_id="1",
        )
    )


class GitHubClientGetFileTests(unittest.TestCase):
    """get_file must not crash on null/empty content from the API."""

    def test_normal_base64(self):
        gh = _make_gh()
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            return _Resp(
                payload={
                    "type": "file",
                    "encoding": "base64",
                    "content": "aGVsbG8=",
                    "size": 5,
                }
            )

        gh.http = SimpleNamespace(get=fake_get)
        self.assertEqual(gh.get_file("o/r", "file.txt"), "hello")
        self.assertEqual(len(calls), 1)

    def test_large_file_falls_back_to_download_url(self):
        gh = _make_gh()
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            if url.startswith("/repos/"):
                return _Resp(
                    payload={
                        "type": "file",
                        "encoding": "base64",
                        "content": None,
                        "size": 2_000_000,
                        "download_url": (
                            "https://raw.githubusercontent.com/o/r/main/big.bin"
                        ),
                    }
                )
            return _Resp(text="BIGCONTENT")

        gh.http = SimpleNamespace(get=fake_get)
        self.assertEqual(gh.get_file("o/r", "big.bin"), "BIGCONTENT")
        self.assertIn("https://raw.githubusercontent.com/o/r/main/big.bin", calls)

    def test_empty_file(self):
        gh = _make_gh()

        def fake_get(url, **kwargs):
            if url.startswith("/repos/"):
                return _Resp(
                    payload={
                        "type": "file",
                        "encoding": "base64",
                        "content": "",
                        "size": 0,
                        "download_url": (
                            "https://raw.githubusercontent.com/o/r/main/empty.txt"
                        ),
                    }
                )
            return _Resp(text="")

        gh.http = SimpleNamespace(get=fake_get)
        self.assertEqual(gh.get_file("o/r", "empty.txt"), "")

    def test_null_without_download_url(self):
        gh = _make_gh()

        def fake_get(url, **kwargs):
            return _Resp(
                payload={
                    "type": "file",
                    "encoding": "base64",
                    "content": None,
                    "size": 0,
                }
            )

        gh.http = SimpleNamespace(get=fake_get)
        self.assertEqual(gh.get_file("o/r", "x.txt"), "")


if __name__ == "__main__":
    unittest.main()
"""Regression tests for the web_fetch redirect-following path.

These cover the two failure modes that the SSRF redirect guard previously
silently swallowed:

* a redirect chain longer than `_MAX_FETCH_HOPS` (it used to return the
  empty redirect body as "content");
* a self-referential / cyclic redirect loop (same).
"""
import httpx
import pytest

import app.tools as tools_mod
from app.tools import ToolContext, make_executor

pytestmark = pytest.mark.usefixtures("_test_env")


class FakeResponse:
    def __init__(self, status_code, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text

    @property
    def is_redirect(self):
        return self.status_code in {301, 302, 303, 307, 308} and "location" in self.headers

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("http error", request=None, response=None)


class FakeClient:
    handler = None
    requested: list = []

    def __init__(self, timeout=None, follow_redirects=False):
        assert follow_redirects is False, "SSRF guard requires manual redirect following"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, **kwargs):
        FakeClient.requested.append(url)
        return FakeClient.handler(url)


@pytest.fixture
def fake_http(monkeypatch):
    FakeClient.requested = []
    monkeypatch.setattr(tools_mod.httpx, "Client", FakeClient)
    # all hosts resolve to a public address so the per-hop SSRF check passes;
    # the redirect guards under test are purely structural (hop count / cycle).
    monkeypatch.setattr(
        tools_mod.socket, "getaddrinfo",
        lambda h, p: [(None, None, None, "", ("93.184.216.34", 0))],
    )
    return FakeClient


def _ctx(tmp_path):
    ctx = ToolContext(workspace=str(tmp_path))
    return ctx, make_executor(ctx)


def test_web_fetch_rejects_too_many_redirects(fake_http, tmp_path):
    # every hop returns a fresh 302 to a brand-new path, so it is neither a
    # cycle nor a redirect to a blocked host, just unboundedly long.
    counter = {"n": 0}

    def handler(url):
        n = counter["n"]
        counter["n"] += 1
        return FakeResponse(302, {"location": f"/hop{n}"})

    fake_http.handler = handler
    ctx, ex = _ctx(tmp_path)
    out = ex("web_fetch", {"url": "http://example.com/start"})

    assert "too many redirects" in out
    # it should have stopped after _MAX_FETCH_HOPS requests, not gone forever
    assert len(FakeClient.requested) == tools_mod._MAX_FETCH_HOPS
    # and never fetched a non-public host
    assert all(u.startswith("http://example.com/") for u in FakeClient.requested)


def test_web_fetch_rejects_self_redirect_loop(fake_http, tmp_path):
    def handler(url):
        if url.endswith("/loop"):
            return FakeResponse(302, {"location": "/loop"})
        return FakeResponse(200, {}, "landing")

    fake_http.handler = handler
    ctx, ex = _ctx(tmp_path)
    out = ex("web_fetch", {"url": "http://example.com/loop"})

    assert "redirect loop detected" in out
    assert FakeClient.requested == ["http://example.com/loop"]


def test_web_fetch_rejects_cyclic_redirect_loop(fake_http, tmp_path):
    # r1 -> r2 -> r1 -> ... (a 2-cycle, not a direct self-redirect)
    def handler(url):
        if url.endswith("/r1"):
            return FakeResponse(302, {"location": "/r2"})
        if url.endswith("/r2"):
            return FakeResponse(302, {"location": "/r1"})
        return FakeResponse(200, {}, "landing")

    fake_http.handler = handler
    ctx, ex = _ctx(tmp_path)
    out = ex("web_fetch", {"url": "http://example.com/r1"})

    assert "redirect loop detected" in out
    assert FakeClient.requested == ["http://example.com/r1", "http://example.com/r2"]
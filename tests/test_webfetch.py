import httpx
import pytest

import app.tools as tools_mod
from app.tools import _is_blocked_url, _url_error, ToolContext, make_executor

pytestmark = pytest.mark.usefixtures("_test_env")


@pytest.mark.parametrize("url", [
    "http://localhost",
    "http://LOCALHOST:8080/",
    "http://127.0.0.1/",
    "http://127.0.0.5:9000/x",
    "http://0.0.0.0/",
    "http://10.0.0.1/",
    "http://172.16.0.9/",
    "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://100.64.0.1/",          # CGNAT range -> is_private
    "http://fd00::1/",
    "http://[::1]/",
    "http://metadata.google.internal/",
    "http://myservice.internal/",
    "http://printer.local/",
    "http://[::ffff:127.0.0.1]/",
    "http://100.64.0.1/",          # CGNAT range -> not globally routable
    "http://198.51.100.9/",        # doc range -> not globally routable
    "not-a-url",
])
def test_blocked_urls(url):
    assert _is_blocked_url(url)


def test_scheme_checked_separately():
    assert _is_blocked_url("ftp://example.com/") is False
    assert _url_error("ftp://example.com/") is not None


def test_dns_resolving_to_private_ip_is_blocked(monkeypatch):
    monkeypatch.setattr(tools_mod.socket, "getaddrinfo",
                        lambda h, p: [(None, None, None, "", ("10.1.2.3", 0))])
    assert _is_blocked_url("http://sneaky-host.example/")
    assert _is_blocked_url("http://127-0-0-1.nip.io/")


def test_dns_failure_is_blocked(monkeypatch):
    def boom(*a, **k):
        raise OSError("no dns")
    monkeypatch.setattr(tools_mod.socket, "getaddrinfo", boom)
    assert _is_blocked_url("http://unknown.invalid/")


def test_dns_resolving_to_public_ip_is_allowed(monkeypatch):
    monkeypatch.setattr(tools_mod.socket, "getaddrinfo",
                        lambda h, p: [(None, None, None, "", ("93.184.216.34", 0))])
    assert not _is_blocked_url("http://example.com/")
    assert _url_error("http://example.com/") is None


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
    return FakeClient


def mk_ctx(tmp_path):
    ctx = ToolContext(workspace=str(tmp_path))
    return ctx, make_executor(ctx)


def test_web_fetch_ok(fake_http, tmp_path):
    fake_http.handler = lambda url: FakeResponse(200, {}, "<html><body>Hi <b>there</b></body></html>")
    ctx, ex = mk_ctx(tmp_path)
    out = ex("web_fetch", {"url": "http://example.com/page"})
    assert "Hi there" in out


def test_web_fetch_blocks_direct_target(fake_http, tmp_path):
    ctx, ex = mk_ctx(tmp_path)
    out = ex("web_fetch", {"url": "http://127.0.0.1:8000/health"})
    assert "error" in out
    assert FakeClient.requested == []


def test_web_fetch_rejects_non_http(fake_http, tmp_path):
    ctx, ex = mk_ctx(tmp_path)
    out = ex("web_fetch", {"url": "file:///etc/passwd"})
    assert "error: url must start with" in out


def test_web_fetch_blocks_redirect_to_private_host(fake_http, monkeypatch, tmp_path):
    # public -> redirect -> host that DNS-resolves to a private IP: must not fetch it
    def handler(url):
        if "public.example" in url:
            return FakeResponse(302, {"location": "http://sneaky-host.example/x"})
        return FakeResponse(200)

    fake_http.handler = handler
    monkeypatch.setattr(tools_mod.socket, "getaddrinfo",
                        lambda h, p: ([(None, None, None, "", ("10.9.9.9", 0))]
                                      if "sneaky-host" in h
                                      else [(None, None, None, "", ("93.184.216.34", 0))]))
    ctx, ex = mk_ctx(tmp_path)
    out = ex("web_fetch", {"url": "http://public.example/start"})
    assert "redirect target is not allowed" in out
    # the private redirect target must never actually be requested
    assert all("sneaky-host.example" not in u for u in FakeClient.requested)


def test_web_fetch_follows_safe_redirect_chain(fake_http, monkeypatch, tmp_path):
    def handler(url):
        if url.endswith("r1"):
            return FakeResponse(302, {"location": "/r2"})
        return FakeResponse(200, {}, "landing")

    fake_http.handler = handler
    monkeypatch.setattr(tools_mod.socket, "getaddrinfo",
                        lambda h, p: [(None, None, None, "", ("93.184.216.34", 0))])
    ctx, ex = mk_ctx(tmp_path)
    out = ex("web_fetch", {"url": "http://example.com/r1"})
    assert out == "landing"
    assert len(FakeClient.requested) == 2

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest


@pytest.fixture(autouse=True)
def _test_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "test-model")
    monkeypatch.setenv("GITHUB_TOKEN", "gh_test_token")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-webhook-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-telegram-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456789")
    monkeypatch.delenv("MAX_ITERATIONS", raising=False)
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main
    monkeypatch.setattr(main, "build_bridge", lambda s: None)
    with TestClient(main.app) as c:
        yield c

import pytest

from app.config import Settings, get_settings


def test_missing_reports_unset_vars():
    s = Settings()
    assert s.missing == [
        "OPENROUTER_API_KEY",
        "GITHUB_TOKEN",
        "GITHUB_WEBHOOK_SECRET",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ]
    assert not s.is_valid


def test_load_reads_env(_test_env):
    s = get_settings()
    assert s.openrouter_api_key == "test-openrouter-key"
    assert s.openrouter_model == "test-model"
    assert s.github_token == "gh_test_token"
    assert s.github_webhook_secret == "test-webhook-secret"
    assert s.telegram_chat_id == "123456789"
    assert s.is_valid


def test_load_strips_whitespace(_test_env):
    s = get_settings()
    assert s.openrouter_api_key == s.openrouter_api_key.strip()


def test_max_iterations_falls_back_on_bad_value(monkeypatch):
    monkeypatch.setenv("MAX_ITERATIONS", "not-a-number")
    from app.config import _get_int
    assert _get_int("MAX_ITERATIONS", 40) == 40


def test_max_iterations_minimum_is_one(monkeypatch):
    monkeypatch.setenv("MAX_ITERATIONS", "0")
    s = Settings.load()
    assert s.max_iterations == 1

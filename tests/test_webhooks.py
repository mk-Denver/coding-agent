import hmac
import hashlib
import json
import threading
import time


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post(client, event: str, payload: dict, secret: str = "test-webhook-secret"):
    body = json.dumps(payload).encode()
    headers = {
        "x-github-event": event,
        "x-hub-signature-256": sign(secret, body),
        "content-type": "application/json",
    }
    return client.post("/webhook/github", content=body, headers=headers), body


def wait_for(fn, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(0.05)
    return fn()


def test_health_reports_config(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["config_valid"] is True
    assert data["model"] == "test-model"


def test_index_lists_routes(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "/webhook/github" in r.json()["webhook"]


def test_webhook_rejects_bad_signature(client):
    r = client.post(
        "/webhook/github",
        content=b"{}",
        headers={"x-hub-signature-256": "sha256=" + "0" * 64, "x-github-event": "ping"},
    )
    assert r.status_code == 401


def test_webhook_rejects_missing_signature(client):
    r = client.post("/webhook/github", content=b"{}", headers={"x-github-event": "ping"})
    assert r.status_code == 401


def test_webhook_ping(client):
    resp, _ = post(client, "ping", {})
    assert resp.status_code == 200
    assert resp.json() == {"status": "pong"}


def test_comment_command_triggers_fix(client, monkeypatch):
    import app.main as main

    started = threading.Event()
    seen = {}

    def fake_fix(settings, gh, repo, number, notify=None):
        seen["args"] = (repo, number)
        started.set()

    monkeypatch.setattr(main.agent, "fix_issue", fake_fix)
    resp, _ = post(client, "issue_comment", {
        "action": "created",
        "repository": {"full_name": "o/r"},
        "issue": {"number": 7},
        "comment": {"user": {"login": "alice"}, "body": "please @agent fix this"},
    })
    assert resp.status_code == 200
    assert resp.json() == {"status": "queued"}
    assert started.wait(5)
    assert seen["args"] == ("o/r", 7)


def test_comment_review_on_pr_triggers_review(client, monkeypatch):
    import app.main as main

    started = threading.Event()
    seen = {}

    def fake_review(settings, gh, repo, number, notify=None):
        seen["args"] = (repo, number)
        started.set()

    monkeypatch.setattr(main.agent, "review_pr", fake_review)
    payload = {
        "action": "created",
        "repository": {"full_name": "o/r"},
        "issue": {"number": 3, "pull_request": {"url": "x"}},
        "comment": {"user": {"login": "bob"}, "body": "@agent review"},
    }
    resp, _ = post(client, "issue_comment", payload)
    assert resp.json() == {"status": "queued"}
    assert started.wait(5)
    assert seen["args"] == ("o/r", 3)


def test_review_on_issue_is_ignored(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main.agent, "review_pr", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    payload = {
        "action": "created",
        "repository": {"full_name": "o/r"},
        "issue": {"number": 3},
        "comment": {"user": {"login": "bob"}, "body": "@agent review"},
    }
    resp, _ = post(client, "issue_comment", payload)
    assert resp.json() == {"status": "ignored"}


def test_agent_goes_to_separate_user_comes_from_bot(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(
        main.agent, "fix_issue",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("self loop")),
    )
    payload = {
        "action": "created",
        "repository": {"full_name": "o/r"},
        "issue": {"number": 3},
        "comment": {"user": {"login": "coding-agent[bot]"}, "body": "@agent fix"},
    }
    resp, _ = post(client, "issue_comment", payload)
    assert resp.json() == {"status": "self"}


def test_pr_labelled_agent_review_queues_review(client, monkeypatch):
    import app.main as main

    started = threading.Event()

    def fake_review(settings, gh, repo, number, notify=None):
        started.set()

    monkeypatch.setattr(main.agent, "review_pr", fake_review)
    resp, _ = post(client, "pull_request", {
        "action": "labeled",
        "repository": {"full_name": "o/r"},
        "pull_request": {"number": 9, "labels": [{"name": "agent:review"}, {"name": "bug"}]},
    })
    assert resp.json() == {"status": "queued"}
    assert started.wait(5)


def test_pr_opened_without_label_not_reviewed(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(
        main.agent, "review_pr",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    resp, _ = post(client, "pull_request", {
        "action": "opened",
        "repository": {"full_name": "o/r"},
        "pull_request": {"number": 9, "labels": []},
    })
    assert resp.json() == {"status": "ignored"}


def test_issue_labelled_agent_queues_fix(client, monkeypatch):
    import app.main as main

    started = threading.Event()
    seen = {}

    def fake_fix(settings, gh, repo, number, notify=None):
        seen["args"] = (repo, number)
        started.set()

    monkeypatch.setattr(main.agent, "fix_issue", fake_fix)
    resp, _ = post(client, "issues", {
        "action": "labeled",
        "repository": {"full_name": "o/r"},
        "issue": {"number": 5},
        "label": {"name": "AGENT"},
    })
    assert resp.json() == {"status": "queued"}
    assert started.wait(5)
    assert seen["args"] == ("o/r", 5)


def test_spawn_failure_notifies_user(client, monkeypatch):
    import app.main as main

    called = []

    def boom(settings, gh, repo, number, notify=None):
        raise RuntimeError("clone exploded")

    monkeypatch.setattr(main.agent, "fix_issue", boom)
    monkeypatch.setattr(main.ctx, "notify", called.append)
    resp, _ = post(client, "issues", {
        "action": "labeled",
        "repository": {"full_name": "o/r"},
        "issue": {"number": 8},
        "label": {"name": "agent"},
    })
    assert resp.json() == {"status": "queued"}
    assert wait_for(lambda: called)
    assert any("Task failed" in m and "clone exploded" in m for m in called)


def test_repoless_payload_ignored(client):
    resp, _ = post(client, "issues", {"action": "labeled", "label": {"name": "agent"}})
    assert resp.json() == {"status": "ignored"}

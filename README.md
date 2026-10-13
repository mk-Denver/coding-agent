# coding-agent

A small autonomous coding agent you host on [Render](https://render.com). It connects to your
GitHub repos with a Personal Access Token, uses an [OpenRouter](https://openrouter.ai) model
for reasoning, and talks to you over **Telegram** so you can assign work and get updates while
you sleep.

It can:
- **Review PRs** (posts a review comment)
- **Fix issues / implement features** (clones the repo, edits code, opens a PR)
- **Answer questions on issues** (inspects the code and posts a grounded reply)

## How it works

```
GitHub webhook ──┐                         ┌──> OpenRouter (LLM)
                 ├──> FastAPI agent loop ──┤
Telegram  <─────┘   (tools: read/edit/grep/run/web) └──> git push + open PR
```

Triggers:
- **GitHub**: comment `@agent fix` / `@agent review` / `@agent answer` on any issue or PR.
  Also: label an issue `agent` to auto-fix it; label a PR `agent:review` to auto-review.
- **Telegram**: send `/fix <owner/repo> <issue#>`, `/review <owner/repo> <pr#>`,
  `/answer <owner/repo> <issue#>`. The agent sends progress updates to your chat.
  Use `/tasks` to see active and recent task progress.

## Project layout

```
app/
  main.py          FastAPI: health + GitHub webhooks, wires Telegram
  agent.py         agentic loop: review_pr / fix_issue / answer_issue
  tools.py         tool schemas + workspace executor (read/edit/grep/run/web)
  github_client.py clone, branch, commit, push, PRs, comments, diffs
  llm.py           OpenRouter client (OpenAI SDK, tool-calling)
  telegram_bot.py  polling bot: outbound updates + inbound commands
  config.py        env-based settings
render.yaml        Render Blueprint
requirements.txt
```

## Setup

### 1. Get the keys
- **OpenRouter**: create a key at https://openrouter.ai/keys. Pick a model id
  (e.g. `anthropic/claude-3.5-sonnet`, `openai/gpt-4o-mini`, or `openrouter/auto`).
- **GitHub**: create a fine-grained PAT at https://github.com/settings/tokens with
  **Contents (R/W)**, **Pull requests (R/W)**, **Issues (R/W)**, **Metadata (R)** on the repos
  you want it to touch.
- **Telegram**: talk to [@BotFather](https://t.me/BotFather), create a bot, copy the token.
  Then send any message to your bot and visit
  `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your numeric `chat.id`.

### 2. Deploy on Render
1. Push this folder to a GitHub repo.
2. In Render: **New > Blueprint**, select the repo. It reads `render.yaml`.
3. Fill the secret env vars when prompted (`OPENROUTER_API_KEY`, `GITHUB_TOKEN`,
   `GITHUB_WEBHOOK_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).
4. Deploy. Confirm `/health` returns `{"config_valid": true}`.

> **Keeping it awake on the free plan:** The app self-pings `/health` every 10 minutes to
> delay Render's sleep timer. For full reliability, also set up a free external monitor
> (e.g. [UptimeRobot](https://uptimerobot.com) or [cron-job.org](https://cron-job.org)) to
> hit `https://<your-service>.onrender.com/health` every 5 minutes — only an external ping
> can wake the service after it has already slept. For zero gaps, use the **Starter** plan
> (~$7/mo, always-on).

### 3. Add the GitHub webhook
Repo **Settings > Webhooks > Add webhook**:
- Payload URL: `https://<your-service>.onrender.com/webhook/github`
- Content type: `application/json`
- Secret: the same `GITHUB_WEBHOOK_SECRET`
- Events: `Issue comments`, `Pull requests`, `Issues`

### 4. Use it
- On a PR or issue, comment: `@agent review` / `@agent fix` / `@agent answer`.
- From Telegram: `/fix owner/repo 42` — you'll get progress updates in chat.

## Run locally
```bash
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt
# set env vars (copy .env.example -> .env and export them)
uvicorn app.main:app --reload --port 8000
```
For local GitHub webhooks use `ngrok http 8000` and point the webhook at the ngrok URL.

## Notes & safety
- The agent clones to a temp dir and pushes a **new branch**; it never pushes to your default branch.
- `run_command` executes shell in the cloned workspace (lint/tests). Keep `MAX_ITERATIONS`
  bounded; the default is 40.
- The `web_fetch` tool blocks localhost, private IP ranges, and cloud metadata endpoints
  (SSRF guard). `web_search` queries DuckDuckGo only.
- Model choice matters a lot for coding quality — prefer a strong coding model.
- This is a personal tool: the PAT is powerful, so scope it to specific repos.

See [SECURITY.md](SECURITY.md) for the full security model and hardening recommendations.

## Contributing & community
- [CONTRIBUTING.md](CONTRIBUTING.md) — setup, dev workflow, PR checklist
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) — community standards
- [SECURITY.md](SECURITY.md) — reporting vulnerabilities and the security model

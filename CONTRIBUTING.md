# Contributing to coding-agent

Thanks for your interest in contributing! This is a small project, so the process is lightweight.

## Getting Started

1. Fork and clone the repo
2. Create a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Windows: .venv\Scripts\activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Copy `.env.example` to `.env` and fill in your credentials
5. Run locally:
   ```bash
   uvicorn app.main:app --reload --port 8000
   ```

## Development Workflow

1. Create a branch: `git checkout -b fix/my-bugfix` or `feat/my-feature`
2. Make your changes
3. Verify everything compiles:
   ```bash
   python -m py_compile app/*.py
   ```
4. If you added new env vars, update `.env.example` and `render.yaml`
5. Commit with a clear message (no emoji unless asked)
6. Open a PR

## Code Style

- Follow existing patterns in the codebase
- No comments unless explicitly necessary
- Use `from __future__ import annotations` (already in all files)
- Keep functions focused and small
- Match the existing typing style (`str | None`, not `Optional[str]`)

## What to Work On

Good first issues:
- Adding new tool types for the agent (e.g. `grep` with ripgrep, `tree` view)
- Improving the DuckDuckGo search parser robustness
- Adding retry/backoff for transient LLM errors
- Adding tests
- Improving the system prompt for better agent behavior

## Pull Request Checklist

- [ ] Code compiles: `python -m py_compile app/*.py`
- [ ] No secrets or tokens committed
- [ ] `.env.example` updated if new env vars were added
- [ ] `render.yaml` updated if deployment config changed
- [ ] No unrelated changes in the PR

## Reporting Bugs

Open a GitHub issue with:
- What you expected
- What actually happened
- Steps to reproduce (commands, env vars, model used)
- Relevant log output (redact any secrets)

## Questions

Open a GitHub issue — no separate Discord/mailing list for this project.

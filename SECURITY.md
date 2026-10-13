# Security Policy

## Supported Versions

Only the latest release on the `main` branch is supported with security fixes.

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Instead, email the maintainer directly or use [GitHub's private vulnerability reporting](https://github.com/<owner>/<repo>/security/advisories/new) (Security tab → Report a vulnerability).

Include:
- A description of the vulnerability
- Steps to reproduce
- The potential impact
- Any suggested fixes

You will receive a response within 72 hours. If the vulnerability is confirmed, a fix and advisory will be published as soon as possible.

## Security Considerations

This project runs an autonomous coding agent that executes shell commands, clones repositories, and posts to GitHub and Telegram. Be aware of the following:

### Secrets

- **Never commit `.env` files.** The `.gitignore` is configured to exclude them.
- The `Settings` dataclass hides secrets from `repr()` output (`field(repr=False)`).
- GitHub tokens are passed via a `GIT_ASKPASS` temp script, not embedded in clone URLs.
- Tool command execution uses a restricted environment (`PATH`, `HOME`, `TMPDIR` only — no secrets).

### Shell Execution

- The `run_command` tool uses `shell=True` with agent-supplied commands. This is intentional for shell semantics (pipes, `&&`).
- Mitigations: restricted env, workspace-confined `cwd`, `start_new_session` for clean process-group kills.
- **Do not expose this agent to untrusted users or untrusted prompts** — the command execution boundary assumes a trusted model.

### Network (SSRF)

- The `web_fetch` tool blocks localhost, private IP ranges, link-local addresses, and cloud metadata endpoints (`169.254.169.254`).
- The `web_search` tool queries DuckDuckGo only.

### Telegram Authorization

- All Telegram command handlers check `update.effective_chat.id` against the configured `TELEGRAM_CHAT_ID`.
- Only the configured chat can issue commands.

### Webhook Verification

- GitHub webhooks are verified via HMAC-SHA256 using `GITHUB_WEBHOOK_SECRET`.
- If the secret is unset, all webhooks are rejected (fail-closed).

### Filesystem Containment

- All file tools (`read_file`, `write_file`, `edit_file`) resolve symlinks via `os.path.realpath` and check containment against the workspace root.
- The `search` tool also validates each file's real path before reading.

## Hardening Recommendations for Production

1. Use the **Render Starter plan** (always-on) so webhooks aren't missed
2. Use a **GitHub App** instead of a Personal Access Token for finer-grained permissions
3. Run the agent in a **container** with no access to host secrets
4. Set `MAX_ITERATIONS` to a reasonable bound (default: 40)
5. Regate the GitHub PAT to specific repositories only
6. Use a strong, random `GITHUB_WEBHOOK_SECRET`

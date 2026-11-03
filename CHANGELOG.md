# Changelog

All notable changes to peerlink. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- `SKILL.md` for remote agents: a follow-up message (`peerlink msg`) to a run of a task with `approve = true` waits for a new code like the run itself, and a `note` letter with `"event": "followup.dropped"` means the message was not approved. Agents get the new text the next time they download `/bundle.tar.gz`.

## [1.0.0] - 2026-11-04

First public release.

### Added

- `peerlink serve`: an HTTP server on your tailnet IP and loopback. Every request is mapped to a principal with `tailscale whois`; unknown callers get 403.
- Task catalog in `~/.peerlink/tasks`: one `task.toml` per task, parameters checked against a schema, hot reload. Task kinds `script`, `pi` and `codex`.
- Agent tasks run `pi --print` or `codex exec` with your own settings and login. The caller's text stays in `request.json` and never goes into the prompt.
- Follow-up questions: an agent task writes `question.txt`, the run becomes `needs_input`, and the caller's answer resumes the same session.
- Owner approval for tasks with `approve = true`: a remote run, and every follow-up message to it, waits for a one-time 6-digit code that peerlink sends to your Telegram.
- Files from runs: a run puts files in `out/`, and the caller downloads them with `GET /runs/<id>/files/<name>`.
- Letters and a webhook doorbell: results wait as letters until the caller acknowledges them, and the doorbell sends only `{"from", "pending"}`.
- Limits: concurrency caps, resource locks, per-day dedupe, daily submit caps, per-task timeouts, and an emergency stop (`peerlink stop --all` / `peerlink resume`).
- Cleanup: when a turn ends, peerlink kills the processes the run left behind.
- Browser requests (`Origin`, `Sec-Fetch-Site`, non-JSON POSTs, unknown `Host` names) are refused.
- `GET /bundle.tar.gz`: a client zipapp and `SKILL.md` for remote agents.
- CLI for the node and for remote agents, `peerlink doctor`, `peerlink task lint` and `peerlink task try`, and `peerlink --version`.
- Example tasks `hello` (script) and `ask` (Pi or Codex, with approval), and a LaunchAgent template for macOS.

[Unreleased]: https://github.com/jokerlin/peerlink/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/jokerlin/peerlink/releases/tag/v1.0.0

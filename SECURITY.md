# Security

The gateway sits between Claude Code and two remote parties. What it promises:

- **Loopback only.** It binds `127.0.0.1` and nothing else, always. There is no flag to
  bind elsewhere, because the Anthropic line relays whatever OAuth token arrives.
- **Your Claude credential goes to Anthropic and nowhere else.** A `claude-*` request that
  carries its own `Authorization`/`x-api-key` is forwarded to `api.anthropic.com` as it
  came. That header is never sent to the upstream, never stored, never logged.
- **The upstream sees only what it needs.** Non-Claude requests go out with headers rebuilt
  from nothing (`content-type`, `anthropic-version`, `anthropic-beta`, `accept`) plus your
  upstream key. No `x-claude-code-*` header, no cookie, and the body's top-level
  `metadata` (where Claude Code puts your claude.ai account and device ids) is removed.
- **Your upstream key never reaches Claude Code.** The launcher puts it in the gateway
  process's environment only.
- **Logs name paths, models and statuses, never header values or bodies.**

Each of these is pinned by a test in `tests/`.

To report a vulnerability, open a GitHub security advisory on this repository or email
security@devino.ca.

# claude-code-hybrid

**Run Claude Code on your Claude subscription, and run its subagents on any other model.**
Kimi K3, GLM, DeepSeek, Qwen, a local Ollama model, anything behind OpenRouter or LiteLLM.
The main session stays real Claude on the login you already have. A subagent whose
`model:` line names another model is served by your own upstream. No API-key swap, no
second terminal, no restart to change models.

[![tests](https://github.com/DevinoSolutions/claude-code-hybrid/actions/workflows/ci.yml/badge.svg)](https://github.com/DevinoSolutions/claude-code-hybrid/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

```
claude-hybrid                 # Claude Code, on your subscription, with a loopback gateway in front
> use the kimi subagent to add tests for app/auth.py
                              # main loop: Claude (Anthropic, your OAuth login)
                              # kimi-k3-worker: Kimi K3 (your upstream), spawned natively
> /model deepseek-chat        # switch the main loop itself, mid-session, no restart
```

## Why this exists

Claude Code talks to exactly **one** base URL, and its subagents inherit it. So you could
either have real Claude (Anthropic) or another provider (`ANTHROPIC_BASE_URL` plus a key),
not both in one session. Every "use model X in Claude Code" guide makes you give up the
subscription and pay per token for Claude too, or run two terminals.

`claude-code-hybrid` is a ~300-line gateway on `127.0.0.1` that answers that one base URL
and splits by model name:

```
                 ┌──────────────────────────────┐
  Claude Code ──►│ claude-code-hybrid  :8290     │
  (your login)   │                              │
                 │  model claude-*  ───────────►│──► api.anthropic.com   (your OAuth/API key, untouched)
                 │  any other model ───────────►│──► YOUR upstream       (your upstream key only)
                 └──────────────────────────────┘
```

It works because of a documented Claude Code rule: **setting `ANTHROPIC_BASE_URL` without a
credential variable keeps your saved claude.ai login as the active credential** (see the
[LLM gateway guide](https://code.claude.com/docs/en/llm-gateway), "Subscriptions and
gateways"). The gateway forwards that credential to Anthropic and to nobody else.

## 60-second start

Prerequisites: Claude Code installed and logged in (`claude` on your PATH), Python 3.11+,
and one upstream that speaks the **Anthropic Messages API** for the other models.

```bash
pip install git+https://github.com/DevinoSolutions/claude-code-hybrid   # or: uv tool install git+https://github.com/DevinoSolutions/claude-code-hybrid
claude-hybrid init        # asks for the upstream URL and key, writes a 0600 config file
claude-hybrid             # starts the gateway if needed, then Claude Code pointed at it
```

Then drop an agent definition into `.claude/agents/` (examples in [`agents/`](agents/)):

```markdown
---
name: kimi-k3-worker
description: Coding worker on Kimi K3. Use when the user says "use kimi".
tools: Read, Edit, Write, Glob, Grep, Bash
model: kimi-k3
---
You are a coding worker. Do the task and return the diff.
```

Say "use the kimi subagent to ..." and Claude Code spawns it natively: same Agent tool, same
tool permissions, same transcript, different model. Anything after `claude-hybrid` goes to
`claude` untouched (`claude-hybrid --continue`, `claude-hybrid -p "..."`).

## Upstreams that work

Any endpoint that accepts `POST <base>/v1/messages` in Anthropic's format. Set
`HYBRID_UPSTREAM_URL` to the base that makes that path correct:

| upstream | `HYBRID_UPSTREAM_URL` | notes |
|---|---|---|
| OpenRouter | `https://openrouter.ai/api` | hundreds of models, one key |
| Moonshot (Kimi) | `https://api.moonshot.ai/anthropic` | Kimi's own Anthropic-compatible endpoint |
| DeepSeek | `https://api.deepseek.com/anthropic` | `deepseek-chat`, `deepseek-reasoner` |
| Zhipu (GLM) | `https://open.bigmodel.cn/api/anthropic` | GLM 4.5 / 4.6 / 5.x |
| Ollama | `http://localhost:11434` | local models, key can be any non-empty string |
| LiteLLM proxy | `http://localhost:4000` | fan out to anything LiteLLM supports |
| your own proxy | whatever it is | this project was extracted from one |

Provider paths and model ids move; check the provider's "use with Claude Code" page for the
current base URL. The key is sent as both `Authorization: Bearer` and `x-api-key`, since
providers differ on which one they read.

## What you get

- **Native subagents on other models.** `model:` in the agent frontmatter is the only switch.
  Claude Code spawns them, tracks them, and shows them exactly as it does Claude subagents.
- **`/model <id>` mid-session.** The main loop can move to an upstream model and back
  without restarting. Set `HYBRID_CUSTOM_MODEL=<id>` to list it in the `/model` picker and
  skip Claude Code's ~15-second availability probe (needed for slow-first-token models).
- **The subscription stays in charge.** No `ANTHROPIC_API_KEY`, no per-token Claude bill.
- **One command.** `claude-hybrid` starts the gateway if it is not running and then `claude`.
  `claude-hybrid status` and `claude-hybrid stop` for the gateway itself.
- **A log that shows the split.** `class=main line=anthropic ...` versus
  `class=subagent line=upstream model=kimi-k3 ...`, with first-byte and total time per turn.

## What leaves your machine, exactly

| request | goes to | with |
|---|---|---|
| `model: claude-*` carrying your own credential | `api.anthropic.com` | same bytes, same headers minus hop-by-hop and edge headers. `anthropic-beta` is kept because OAuth needs it. |
| everything else (`kimi-k3`, `/v1/models`, OpenAI-dialect paths) | `HYBRID_UPSTREAM_URL` | headers rebuilt from nothing (`content-type`, `anthropic-version`, `anthropic-beta`, `accept`) plus your upstream key. No `x-claude-code-*`, no cookie, **never your Claude credential**, and the body's `metadata` (your claude.ai account and device ids) removed. |

The gateway binds loopback only and has no option to do otherwise. Nothing logs a header
value or a body. See [SECURITY.md](SECURITY.md); every line of that table is a test.

## Configuration

Environment variables win over the config file (`claude-hybrid init` writes it;
`claude-hybrid status` prints its path).

| variable | meaning | default |
|---|---|---|
| `HYBRID_UPSTREAM_URL` | base URL serving every non-Claude model | required |
| `HYBRID_UPSTREAM_KEY` | its key | required |
| `HYBRID_PORT` | gateway port | `8290` |
| `HYBRID_CUSTOM_MODEL` | model id to add to the `/model` picker without the availability probe | unset |
| `HYBRID_CUSTOM_MODEL_NAME` | its label in the picker | `<id> (hybrid)` |
| `HYBRID_ANTHROPIC_URL` | where `claude-*` goes | `https://api.anthropic.com` |

Run the gateway by itself (a service, a container, a different launcher):

```bash
HYBRID_UPSTREAM_URL=https://openrouter.ai/api HYBRID_UPSTREAM_KEY=sk-or-... python -m claude_code_hybrid.gateway
# then, in another shell, with NO ANTHROPIC_API_KEY set:
ANTHROPIC_BASE_URL=http://127.0.0.1:8290 CLAUDE_CODE_GATEWAY_HINT_HEADERS=1 claude
```

## FAQ

**Does this bypass anything?** No. It uses the documented `ANTHROPIC_BASE_URL` gateway
mechanism, your own Anthropic login goes only to Anthropic, and your own upstream key goes
only to your upstream. It is the same split as running two clients, in one process.

**Does the main session have to be Claude?** No. `/model kimi-k3` moves the main loop to the
upstream; `/model opus` brings it back. A subagent with `model: opus` in a hybrid session is
real Claude Opus on your subscription.

**Can I route by agent name instead of model?** Claude Code does not send the agent name,
only the model id (and `x-claude-code-agent-id`, which the gateway logs but never routes on).
So the name is a convention: `kimi-k3-worker` with `model: kimi-k3`.

**Why not [claude-code-router](https://github.com/musistudio/claude-code-router)?** It is a
great general router, but it replaces the credential, so the Claude turns bill an API key.
This project keeps the subscription for Claude and only moves the rest. The closest prior art
is [subagent-handoff](https://github.com/1morr/subagent-handoff) (Node), which does the same
split; this one is Python, ships a launcher and a test suite, and came out of running fifty
Kimi K3 subagents at once behind it.

**Windows?** Yes. The launcher, gateway and tests run on Windows, macOS and Linux (CI matrix).

**Does it slow Claude down?** One loopback hop and a streamed relay; first-byte overhead is
milliseconds. Long thinking turns are not timed out by the gateway (only the connect is).

## For AI coding agents

If you are an agent asked to set this up for a user, do exactly this:

1. `pip install git+https://github.com/DevinoSolutions/claude-code-hybrid` (or `uv tool install` the same URL).
2. Ask the user once for their upstream base URL and key; run `claude-hybrid init --upstream-url <url> --upstream-key <key>`.
3. Copy the agent files the user wants from `agents/` into the project's `.claude/agents/`, editing `model:` to an id the upstream serves.
4. Tell the user to start sessions with `claude-hybrid` instead of `claude`, and to say "use the <name> subagent" to spawn one.
5. Verify: `claude-hybrid status` shows the gateway up; a one-line `claude-hybrid -p "say hi"` returns; the gateway log (`claude-hybrid status` prints its path) shows `line=anthropic` for that turn.

Machine-readable summary: [`llms.txt`](llms.txt).

## Development

```bash
pip install -e ".[test]"
pytest -q
```

MIT. Extracted from a production proxy at [Devino](https://devino.ca) where it runs Claude Code
main loops on a subscription with dozens of concurrent Kimi K3 / GLM subagents.

<sub>Keywords, for the search engines and the agents that read them: Claude Code multi-model,
Claude Code subagent different model, run Claude Code subagents on Kimi K3 / DeepSeek / GLM /
Qwen / Ollama, Claude Code ANTHROPIC_BASE_URL keep subscription, Claude Code LLM gateway,
Claude Code OpenRouter subagents, mix models in Claude Code, hybrid Claude Code session.</sub>

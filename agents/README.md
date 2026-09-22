# Example subagents

Copy the ones you want into your project's `.claude/agents/` (or `~/.claude/agents/` for
every project). The only line that matters to the gateway is `model:`. Whatever id you put
there that does not start with `claude-` is sent to your upstream, so set it to an id your
upstream serves:

| upstream | example `model:` values |
|---|---|
| OpenRouter | `moonshotai/kimi-k2`, `z-ai/glm-4.5`, `deepseek/deepseek-chat`, `qwen/qwen3-coder` |
| Moonshot (Kimi) | `kimi-k2-0905-preview` or whatever the current id is |
| DeepSeek | `deepseek-chat`, `deepseek-reasoner` |
| Zhipu (GLM) | `glm-4.5`, `glm-4.6` |
| Ollama | any pulled tag: `qwen3-coder`, `gpt-oss:20b`, `deepseek-r1:14b` |
| LiteLLM / your own proxy | whatever names the proxy routes |

Naming rule that pays off: put the model in the agent's **name** (`kimi-k3-worker`,
`deepseek-worker`) so "use the kimi subagent" in chat always lands on the same model.
Claude Code never sends the agent name to the gateway, only the `model:` id.

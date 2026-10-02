# OpenCode ACP Provider for Hermes Agent

Connect Hermes Agent to [OpenCode](https://opencode.ai) via the [Agent Client Protocol (ACP)](https://github.com/agentclientprotocol/agent-client-protocol). This lets Hermes use OpenCode as a coding agent backend — same protocol as Copilot ACP, but driving OpenCode's agentic loop instead.

## What it does

When configured, Hermes spawns `opencode acp` as a subprocess and communicates via JSON-RPC 2.0 over stdio (ACP v1). Hermes keeps a **persistent** ACP session (one `opencode` process per model, reused across turns):

1. Spawns `opencode acp` and handshakes (initialize → session/new)
2. First turn: sends the full conversation as a single prompt
3. Later turns: sends only the new user message (or the tool-roundtrip tail)
4. Returns an OpenAI-compatible completion, including parsed tool calls

OpenCode handles model selection and plugin fallbacks — Hermes drives the ACP wire. Tool calls OpenCode emits in its native `<tool_call><parameter=name>…</parameter><parameter=arguments>{json}</parameter></tool_call>` XML are parsed back into OpenAI function-call shape so Hermes's tool loop can execute them.

## Prerequisites

- **OpenCode CLI** installed and on PATH (or set `OPENCODE_BIN`)
  ```bash
  npm i -g opencode-ai@latest
  # or
  brew install anomalyco/tap/opencode
  ```
- **OpenCode auth configured**: `opencode auth login` or set provider env vars (OPENROUTER_API_KEY, etc.)
- **Verify**: `opencode run 'hello'` should work standalone

## OpenCode V1 and V2

Works with both generations (verified live on opencode 1.18.x and 2.0.20):

- The ACP wire protocol is unchanged between generations — same `opencode acp`
  command, JSON-RPC 2.0 over stdio, ACP v1.
- **Model ids differ.** V1 advertised bare model names (`deepseek-v4-flash`);
  V2 advertises full `provider/model` ids (`opencode/deepseek-v4-flash`,
  `openai/gpt-4o-mini`) and validates `session/set_config_option` against that
  catalog. The client resolves requests against the session's advertised
  catalog (exact → case-insensitive → V1 bare-name forms, preferring the
  OpenCode-hosted `opencode/…` tier on ambiguity), so old V1-style entries keep
  working and only catalog-exact ids are ever sent.
- **V2 allows one active prompt per session** — the client serializes the
  switch + prompt region per shared session.
- **A model being in the catalog does not mean it can run.** V2 advertises
  every known model, including ones whose provider you are not signed into;
  those are refused at prompt time. The client surfaces that as a clear
  `Model unavailable` error with the `opencode auth login` fix instead of
  OpenCode's bare internal error.

## Install

```bash
REPO=~/hermes-opencode-acp  # adjust if cloned elsewhere

# Option A — installer script (idempotent, safe to re-run)
"$REPO/install-plugin.sh"

# Option B — manual (same thing the script does)
mkdir -p ~/.hermes/plugins/model-providers
rm -rf ~/.hermes/plugins/model-providers/opencode-acp
cp -R "$REPO/plugin" ~/.hermes/plugins/model-providers/opencode-acp
```

No Hermes-core patch is needed: the plugin supplies its own ACP client
through the `create_client` provider seam (same mechanism as Hermes's
built-in `copilot-acp`), so `hermes update` can never break the install.
Then enable it:

```bash
hermes plugins enable opencode-acp-provider
hermes plugins validate ~/.hermes/plugins/model-providers/opencode-acp
```

## Configure

### Via `hermes setup` / `hermes model`

```bash
hermes setup
# Select "OpenCode" → "OpenCode ACP"
```

### Via `config.yaml`

```yaml
model:
  provider: opencode-acp
  base_url: acp://opencode
  api_mode: chat_completions
```

### Via environment variables

```bash
# Override the OpenCode binary path
export OPENCODE_BIN=/path/to/opencode
# or
export HERMES_OPENCODE_ACP_COMMAND=/path/to/opencode

# Override ACP args (default: ["acp"])
export HERMES_OPENCODE_ACP_ARGS="acp"

# Override the ACP base URL marker
export OPENCODE_ACP_BASE_URL="acp://opencode"
```

## How it works

```
Hermes                         OpenCode
  │                               │
  │── spawn: opencode acp ──────>│
  │                               │
  │<── JSON-RPC initialize ──────│  (handshake)
  │── initialize response ──────>│
  │                               │
  │<── session/new ──────────────│  (create session)
  │── sessionId ────────────────>│
  │                               │
  │<── session/prompt ───────────│  (send conversation)
  │   agent_thought_chunk ...     │  (streaming reasoning)
  │   agent_message_chunk ...     │  (streaming text)
  │<── stopReason: end_turn ────│
  │                               │
  │── OpenAI-compatible ─────────│  (return to Hermes)
  │   completion object           │
```

## Differences from Copilot ACP

| | Copilot ACP | OpenCode ACP |
|---|---|---|
| Binary | `copilot` | `opencode` |
| ACP args | `--acp --stdio` | `acp` |
| Provider slug | `copilot-acp` | `opencode-acp` |
| Base URL marker | `acp://copilot` | `acp://opencode` |
| Auth | Copilot CLI login | OpenCode auth |
| Model selection | GitHub Copilot catalog | OpenCode's config |

## Troubleshooting

**"Could not find the OpenCode CLI command"**
- Install OpenCode: `npm i -g opencode-ai@latest`
- Or set `OPENCODE_BIN=/full/path/to/opencode`

**OpenCode ACP exits immediately**
- Check `opencode run 'hello'` works standalone
- Check OpenCode auth: `opencode providers`

**No streaming text**
- The model may not support streaming via ACP
- Try a different model in OpenCode's config

**"requested model '…' is not in this session's model catalog"**
- OpenCode 2.x validates model ids against the catalog it advertises for the
  session (full `provider/model` ids). The old id may be retired upstream —
  `opencode/mimo-v2.5-free`, for example, is now `opencode/mimo-v2.6-flash-free`.
- Pick a live id: `hermes model` (probes the real catalog) or the exact id
  from a fresh session.

**"model '…' is listed in the session's catalog but the server refused to run it"**
- V2 advertises models whose provider you are not signed into; they only fail
  at prompt time. Run `opencode auth login`, or pick a model from a provider
  you are signed into (e.g. the `opencode/…` tier).

## License

MIT
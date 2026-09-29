# jambu-gpu

Provider-agnostic CLI for provisioning and managing **temporary GPU runtimes** for
Jambu.ai Lab projects.

`jambu.yaml` declares *what should run and how long it may live*. The CLI provisions
compute from the configured provider, starts the model server, runs your workloads
against it, and guarantees the GPU does not stay billable once nobody is using it.

`jambu.yaml` is a **shared manifest** across Jambu.ai Lab sub-projects: this tool
only reads its own `gpu_runtime:` key, so the same file can carry other Jambu tools'
config alongside it without collision. A dedicated, gpu-runtime-only `config.yml`
(no wrapper key) is still supported for solo use — see [below](#jambuyaml).

The first provider adapter is **vast.ai**. Adding another provider means implementing
the provider contract — no change to lifecycle, execution or CLI logic.

---

## Quick start

```bash
pip install -e .

cd your-experiment
jambu-gpu init --model empero-ai/Qwythos-9B-v2

export VAST_API_KEY=...          # the only key you need
export HF_TOKEN=...              # optional, for gated HuggingFace models

jambu-gpu validate               # fails before provisioning anything
jambu-gpu offers                 # what is available and what it costs
jambu-gpu setup                  # provision + watchdog + model server
jambu-gpu run python experiment.py
jambu-gpu status
jambu-gpu destroy
```

`VAST_API_KEY` is read from the environment, a project-local `.env`, or
`~/.jambu/credentials` — never from `jambu.yaml`.

---

## Commands

| Command | What it does |
| --- | --- |
| `jambu-gpu init` | Write a starter `jambu.yaml` and `.env.example` (`--flat` for legacy `config.yml`) |
| `jambu-gpu inspect-model <repo>` | Suggest `min_vram_gb`/`dtype`/`tool_calling` for a HuggingFace model, with sources cited (`--add-profile <name>` writes it into the model catalog) |
| `jambu-gpu profiles` | List the model profiles available in `jambu.models.yaml` |
| `jambu-gpu providers` | Provider capability matrix and credential status |
| `jambu-gpu validate` | Schema, policy, credentials, capabilities, real capacity |
| `jambu-gpu offers` | Matching GPU offers and their hourly price |
| `jambu-gpu setup` | Provision, install watchdog, start runtime, wait for READY |
| `jambu-gpu run <cmd>` | Guarantee READY, then run the command with heartbeats |
| `jambu-gpu status` | Reconcile local + provider state, show the guard decision |
| `jambu-gpu lock --for 2h` | Prevent automatic shutdown until the TTL expires |
| `jambu-gpu unlock` | Resume normal lifecycle rules |
| `jambu-gpu stop` | Stop compute (idempotent) |
| `jambu-gpu destroy` | Destroy the remote resource (idempotent) |
| `jambu-gpu logs` | Provider-side container logs |
| `jambu-gpu events` | Structured lifecycle event log |
| `jambu-gpu guard` | Evaluate/enforce the lifecycle rule from cron or CI |
| `jambu-gpu endpoint` | Print the model endpoint URL |

Every command accepts `--json` for machine-readable output and `--config PATH`.

---

## jambu.yaml

`jambu-gpu` reads only the `gpu_runtime:` key — everything else in the file is
another Jambu Lab tool's business:

```yaml
# jambu.yaml
gpu_runtime:
  version: 1

  provider:
    name: vast
    profile: default

  compute:
    gpu:
      min_vram_gb: 24
      count: 1
    disk_gb: 80

  model:
    id: empero-ai/Qwythos-9B-v2

  runtime:
    engine: vllm
    port: 8000
    context_length: 16384

  lifecycle:
    max_lifetime: 6h
    idle_timeout: 15m
    auto_stop: true
    auto_destroy: false
    cleanup_on_setup_failure: true
    allow_indefinite_lock: false
    lock:
      enabled: false
    watchdog:
      enabled: true
      port: 8777
      interval: 60s

  health:
    interval: 60s
    startup_timeout: 10m

  budget:
    max_hourly_cost_usd: 1.00
    max_session_cost_usd: 5.00

# some_other_jambu_tool:
#   ...its own config, untouched by jambu-gpu...
```

Don't need to share the file? `jambu-gpu init --flat` writes a plain `config.yml`
with the same fields at the top level, no `gpu_runtime:` wrapper — still fully
supported, just not the shared-manifest shape. `jambu.yaml` is searched first when
both exist in a directory.

**Running (or planning to run) more than one model?** Don't copy this whole
file per model — set `model_profile: <name>` instead of `model:`/`compute:`
directly, and keep the actual model definitions in one shared
`jambu.models.yaml` catalog:

```yaml
gpu_runtime:
  version: 1
  model_profile: qwythos-9b   # from jambu.models.yaml - the only line that changes
  lifecycle: {idle_timeout: 15m, max_lifetime: 6h}
  budget: {max_hourly_cost_usd: 1.50}
```

`jambu-gpu inspect-model <repo> --add-profile <name>` writes straight into the
catalog; `jambu-gpu profiles` lists what's in it. See
[`docs/CHOOSING_A_MODEL.md`](docs/CHOOSING_A_MODEL.md#dont-write-a-new-jambuyaml-per-model---use-the-catalog).

Full field reference: [`docs/CONFIG.md`](docs/CONFIG.md). Not sure what values
to put here for a given model? `jambu-gpu inspect-model <repo>` reads the
model's real HuggingFace metadata and suggests them — see
[`docs/CHOOSING_A_MODEL.md`](docs/CHOOSING_A_MODEL.md) for exactly where each
number comes from.

---

## How the cost protection actually works

The CLI process cannot be responsible for idle shutdown: close the laptop and a local
watchdog dies while the GPU keeps billing. So `setup` installs a **watchdog on the
instance itself**:

```
CLI ──provisions──> instance
                      │
                      ├── watchdog (HTTP :8777, authenticated)
                      │     ├── holds the activity/heartbeat state
                      │     ├── evaluates the lifecycle policy every 60s
                      │     └── calls the provider API to stop/destroy itself
                      └── vLLM :8000
```

The watchdog script is generated at provision time: the **lifecycle policy source is
inlined verbatim from `jambu_gpu/core/policy.py`**, so the remote guard and
`jambu-gpu status` can never disagree. The provider adapter injects only the two calls
needed to stop or destroy the machine.

An instance stays billable only while:

1. a workload is running (heartbeat alive), or
2. a workload is queued / the model is still starting, or
3. an explicit lock is active.

Anything else transitions to `STOPPED` (or `DESTROYED`) with the reason recorded.

**What counts as activity** — a running workload, a queued workload, model startup, an
active lock. Explicitly *not* activity: a healthy vLLM server, a health check, or an
open SSH session. Otherwise an idle-but-healthy server would keep the GPU alive forever.

A crashed CLI, a dropped SSH session or a sleeping laptop stops the heartbeat; after
`lifecycle.heartbeat_grace` the workload is treated as stale, the instance goes IDLE,
and the normal idle rules apply.

### Second enforcement path

If you want belt and braces, run the guard from anywhere with API access:

```bash
*/5 * * * * cd /path/to/experiment && jambu-gpu guard
```

It evaluates the same policy against reconciled state and enforces it.

---

## Security note on the watchdog credential

For the watchdog to stop its own machine it needs provider API access, so
`VAST_API_KEY` is written to the instance at `/etc/jambu/watchdog.json` (mode 600).

Use a **dedicated, restricted Vast.ai API key** for this, not your personal key. Set
`lifecycle.watchdog.enabled: false` to opt out — but then shutdown depends on
`jambu-gpu guard` or the CLI staying alive, and `validate` will warn you.

---

## Architecture

```
jambu.yaml (gpu_runtime: key)
    │
    ▼
Jambu GPU CLI
    │
    ├── Lifecycle Controller   core/policy.py   (the only shutdown rule)
    ├── Runtime Manager        runtime/vllm.py  (how the model runs)
    └── State Manager          core/state.py    (.jambu/state.json)
                │
        Provider Contract      providers/base.py
                │
        ┌───────┴────────┐
        ▼                ▼
    Vast adapter    future adapter
```

Four independent concerns: **model**, **inference runtime**, **lifecycle policy**,
**infrastructure provider**. The core never branches on a provider name — only on
`provider.capabilities`.

```
jambu_gpu/
├── cli/          setup, run, status, lifecycle, providers, validate
├── core/         config, models, policy, state, events, engine, execution, health
├── providers/    base, registry, vast/{provider,client,mapper,config}
├── runtime/      base, vllm
└── watchdog/     builder, agent_body   (assembled and shipped to the instance)
```

Adding a provider: [`docs/ADDING_A_PROVIDER.md`](docs/ADDING_A_PROVIDER.md).

---

## Running a workload

`jambu-gpu run` defaults to executing the command **locally** against the remote model
endpoint, and injects:

| Variable | Value |
| --- | --- |
| `JAMBU_GPU_ENDPOINT` | `http://<ip>:<port>` |
| `OPENAI_BASE_URL` | `http://<ip>:<port>/v1` |
| `OPENAI_API_KEY` | `runtime.api_key`, else `jambu-local` |
| `JAMBU_MODEL_ID` | `model.id` |
| `JAMBU_INSTANCE_ID`, `JAMBU_WORKLOAD_ID`, `JAMBU_PROVIDER` | identity |
| `JAMBU_SSH_HOST`, `JAMBU_SSH_PORT` | when the provider exposes SSH |

so an experiment script is just:

```python
from openai import OpenAI
client = OpenAI()                       # picks up OPENAI_BASE_URL / OPENAI_API_KEY
print(client.chat.completions.create(
    model=os.environ["JAMBU_MODEL_ID"],
    messages=[{"role": "user", "content": "hi"}],
).choices[0].message.content)
```

Set `workload.mode: remote` to execute the command on the instance over SSH instead.

The instance is **not** stopped after each command — `idle_timeout` governs reuse, so
sequential experiments don't re-provision.

---

## Agentic flows: nothing to build

vLLM's OpenAI-compatible server already implements both agent-facing APIs. jambu-gpu
does not add an orchestration layer on top of it — it only turns on the right vLLM
flags and hands your workload the same endpoint it always has:

- **`/v1/chat/completions`** with `tools=[...]` — the standard OpenAI tool-calling shape
  every agent framework (LangChain, LangGraph, CrewAI, ...) already speaks.
- **`/v1/responses`** — the OpenAI **Responses API**, byte-compatible with the official
  `openai` SDK's `client.responses.create(...)`, including **MCP tool routing done
  server-side by vLLM** when you set `runtime.tool_server`.

Turning it on is one config field:

```yaml
runtime:
  engine: vllm
  # auto (default) guesses a --tool-call-parser from model.id and turns tool
  # calling on when recognized; set explicitly for anything vLLM doesn't guess.
  tool_calling: auto
  # tool_call_parser: hermes        # override, see docs/CONFIG.md for the list
  # tool_server: https://your-mcp-server   # let vLLM execute MCP tools itself
```

`jambu-gpu run` already exports `JAMBU_GPU_ENDPOINT`, `JAMBU_MODEL_ID` and
`OPENAI_API_KEY` — the whole integration surface. Any OpenAI-SDK-compatible agent
framework reads those and just works, unmodified, against the instance jambu-gpu
provisioned.

**[`examples/mastra-agent/`](examples/mastra-agent/)** is a full, installable
[Mastra](https://mastra.ai) agent project built on those three env vars — a real
tool call round trip (the actual pattern from
[Mastra's own tool docs](https://github.com/mastra-ai/mastra/blob/main/docs/src/content/en/docs/agents/tools.mdx)),
not a hand-rolled loop:

```bash
jambu-gpu setup
jambu-gpu run npm --prefix examples/mastra-agent start
```

Use `jambu-gpu validate` / `jambu-gpu status` to confirm which tool-call parser is
active (`vLLM :8000 (model, tools=hermes)`) — Mastra's tool calls only fire once
one is.

---

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                       # everything, no network access required
.venv/bin/pytest tests/test_policy.py  # the shutdown rule on its own
.venv/bin/pytest tests/test_watchdog.py     # the generated guard, as a real process
.venv/bin/pytest tests/test_integration.py  # setup -> boot -> run -> auto-stop
```

The integration suite does not mock the boot: it **executes the real onstart script
the engine generated**, with absolute paths rewritten into a tmp dir, boots the real
watchdog agent, waits on a real health endpoint, runs a real workload with real
heartbeats, and asserts the guard shuts the instance down with the right reason.

- [`docs/CONFIG.md`](docs/CONFIG.md) — every jambu.yaml field
- [`docs/CHOOSING_A_MODEL.md`](docs/CHOOSING_A_MODEL.md) — where the model-specific values come from, and `jambu-gpu inspect-model`
- [`docs/ADDING_A_PROVIDER.md`](docs/ADDING_A_PROVIDER.md) — the contract, step by step
- [`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md) — each spec guarantee, mapped to its code and test
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — setup, tests, and what a change is allowed to touch

Spec: `GPU Runtime CLI — Provider-Agnostic Provisioning & Lifecycle Spec.md`.

## License

[MIT](LICENSE).

---

<p align="center">
  <a href="https://jambu.ai">
    <img src="docs/assets/jambu-logo.jpg" alt="Jambu.ai — Applied Intelligence Lab" width="280">
  </a>
</p>

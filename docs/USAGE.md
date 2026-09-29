# Using gpu

Operator reference. The project front page is the [README](../README.md). Field-by-field config is [CONFIG.md](CONFIG.md).

## Commands

| Command | What it does |
| --- | --- |
| `gpu init` | Write a starter `jambu.yaml` and `.env.example` (`--flat` for a legacy `config.yml`) |
| `gpu inspect-model <repo>` | Suggest `min_vram_gb`, `dtype`, and `tool_calling` for a HuggingFace model, with sources cited. `--add-profile <name>` writes it into the model catalog |
| `gpu profiles` | List the model profiles in `jambu.models.yaml` |
| `gpu providers` | Provider capability matrix and credential status |
| `gpu validate` | Schema, policy, credentials, capabilities, real capacity. Fails before provisioning |
| `gpu offers` | Matching GPU offers and their hourly price |
| `gpu setup` | Provision, install the watchdog, start the runtime, wait for READY |
| `gpu run <cmd>` | Guarantee READY, then run the command with heartbeats |
| `gpu status` | Reconcile local and provider state, show the guard decision |
| `gpu lock --for 2h` | Prevent automatic shutdown until the TTL expires |
| `gpu unlock` | Resume normal lifecycle rules |
| `gpu stop` | Stop compute. Idempotent |
| `gpu destroy` | Destroy the remote resource. Idempotent |
| `gpu logs` | Provider-side container logs |
| `gpu events` | Structured lifecycle event log |
| `gpu guard` | Evaluate and enforce the lifecycle rule from cron or CI |
| `gpu endpoint` | Print the model endpoint URL |

Every command accepts `--json` and `--config PATH`.

## The manifest

`gpu` reads only the `gpu_runtime:` key. Everything else in `jambu.yaml` belongs to another tool. Credentials never go in the file.

A project that does not share the file can run `gpu init --flat`. That writes a `config.yml` with the same fields at the top level, without the `gpu_runtime:` wrapper. When both files exist, `jambu.yaml` wins.

More than one model: do not copy the whole file per model. Set `model_profile: <name>` and keep the definitions in [`jambu.models.yaml`](../jambu.models.yaml). `gpu inspect-model <repo> --add-profile <name>` writes a profile. `gpu profiles` lists them. See [CHOOSING_A_MODEL.md](CHOOSING_A_MODEL.md).

The full field list is [CONFIG.md](CONFIG.md). A filled example is [`jambu.example.yaml`](../jambu.example.yaml).

## Running a workload

`gpu run` executes the command locally, against the remote model, and injects:

| Variable | Value |
| --- | --- |
| `JAMBU_GPU_ENDPOINT` | `http://<ip>:<port>` |
| `OPENAI_BASE_URL` | `http://<ip>:<port>/v1` |
| `OPENAI_API_KEY` | `runtime.api_key`, otherwise `jambu-local` |
| `JAMBU_MODEL_ID` | `model.id` |
| `JAMBU_INSTANCE_ID`, `JAMBU_WORKLOAD_ID`, `JAMBU_PROVIDER` | identity |
| `JAMBU_SSH_HOST`, `JAMBU_SSH_PORT` | when the provider exposes SSH |

```python
import os
from openai import OpenAI

client = OpenAI()
print(client.chat.completions.create(
    model=os.environ["JAMBU_MODEL_ID"],
    messages=[{"role": "user", "content": "hi"}],
).choices[0].message.content)
```

Set `workload.mode: remote` to run the command on the instance over SSH.

The instance is not stopped after each command. `idle_timeout` governs reuse, so the next experiment does not provision again.

## Agents

vLLM already speaks the agent APIs. gpu does not add an orchestration layer. It turns on the right vLLM flags and hands the workload the same endpoint.

- `/v1/chat/completions` with `tools=[...]` — the OpenAI tool-calling shape used by LangChain, LangGraph, CrewAI, and others.
- `/v1/responses` — the OpenAI Responses API, including MCP tool routing done by vLLM when `runtime.tool_server` is set.

```yaml
runtime:
  engine: vllm
  tool_calling: auto
  # tool_call_parser: hermes
  # tool_server: https://your-mcp-server
```

`tool_calling: auto` guesses a parser from `model.id`. Set `tool_call_parser` when vLLM cannot guess. The list is in [CONFIG.md](CONFIG.md).

[`examples/mastra-agent/`](../examples/mastra-agent/) is a Mastra agent that uses the injected environment variables:

```bash
gpu setup
gpu run npm --prefix examples/mastra-agent start
```

`gpu status` shows the active parser, for example `vLLM :8000 (model, tools=hermes)`. Tool calls fire only after a parser is on.

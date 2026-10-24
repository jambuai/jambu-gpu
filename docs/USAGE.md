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
| `gpu ls` | Every jambu instance in the provider account, and whether this project tracks it |
| `gpu sweep` | Account-wide safety net: report (or `--apply`) stops and destroys of instances whose guard failed |
| `gpu sweep --install-timer` | Write a systemd user timer running `sweep --apply` and this project's `guard` |
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

## Keeping the account clean

The watchdog guards an instance from the inside, and `gpu guard` guards the one instance a project tracks. Neither helps when the watchdog died and the project directory is gone. `gpu ls` and `gpu sweep` look at the whole provider account instead. They only ever see instances whose label starts with `jambu-`, the label `gpu setup` gives every instance. Nothing else in the account is listed or touched.

```bash
gpu ls
```

```
Vast.ai instances
 ID        LABEL                          STATE    GPU          $/H     AGE    TRACKED
 31337001  jambu-Qwythos-9B-v2-3fa2c1     READY    1x RTX 4090  $0.350  7h30m  yes
 31337002  jambu-Llama-3.1-8B-9b01de      STOPPED  1x RTX 4090  $0.350  1d6h   no
```

`gpu sweep` applies two rules:

| Instance | Rule | Action |
| --- | --- | --- |
| running | older than `max_lifetime` + 30m: the watchdog evidently failed | `stop` when this project tracks it, `destroy` otherwise |
| stopped | first seen stopped more than `stopped_ttl` ago | `destroy` |

It is a dry run until you pass `--apply`:

```
$ gpu sweep
> sweeping Vast.ai: running > 6h30m, stopped > 1d (dry run)
- would destroy 31337002 (jambu-Llama-3.1-8B-9b01de): stopped_ttl, stopped for 1d2h
! 1 action(s) pending; re-run with --apply
```

Thresholds come from the nearest `jambu.yaml`. Without one, `--max-lifetime` and `--stopped-ttl` default to `6h` and `24h`. They apply to every jambu instance in the account, so sweep from the project with the longest `max_lifetime`, or pass the flags. Credentials resolve the usual way: the environment, `.env` in the working directory, then `~/.jambu/credentials`.

Providers do not report when an instance stopped, so the sweep keeps the time it first saw each one stopped in `~/.jambu/sweep.json` (`$JAMBU_HOME` overrides `~/.jambu`). An entry goes away when the instance runs again or disappears. `stopped_ttl` therefore counts from the first sweep that saw the instance stopped. Every action is recorded as a `sweep_action` event with its reason, in `~/.jambu/logs/events.jsonl` and in the project's event log when there is one. A local lock on the tracked instance is respected.

### Run it on a timer

```bash
gpu sweep --install-timer            # every 5m; --every 15m to change
systemctl --user daemon-reload && systemctl --user enable --now jambu-gpu-sweep.timer
```

`--install-timer` writes `~/.config/systemd/user/jambu-gpu-sweep.service` and `.timer`. The service runs `python -m jambu_gpu sweep --apply` with the interpreter that installed it. When you run it inside a project, it also runs `gpu --config <abs path> guard` for that project. Installing from a second project adds its guard line and keeps the first. A user timer needs no root, but it stops at logout unless lingering is on (`loginctl enable-linger $USER`). Put the API key in `~/.jambu/credentials` or the project `.env`, because the unit does not inherit your shell's environment.

```bash
gpu sweep --uninstall-timer
systemctl --user disable --now jambu-gpu-sweep.timer
```

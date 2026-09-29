# jambu.yaml reference

`jambu.yaml` is the **desired-state source of truth**. It describes what should run
and how long it may live. It must never contain credentials, and it never contains
provider API details.

`jambu.yaml` is a **shared manifest** across Jambu.ai Lab sub-projects: `jambu-gpu`
reads only its own `gpu_runtime:` top-level key and ignores every other key in the
document, so other Jambu tools can keep their own config in the same file:

```yaml
# jambu.yaml
gpu_runtime:
  version: 1
  model: {id: ...}
  ...

some_other_jambu_tool:
  ...      # not read by jambu-gpu, and jambu-gpu never errors on it
```

Don't need to share the file? `jambu-gpu init --flat` writes a `config.yml` where
the whole document *is* the gpu-runtime config (no `gpu_runtime:` wrapper) — still
fully supported. A bare document with no `gpu_runtime:` key is accepted under the
`jambu.yaml` filename too, for a solo project that just prefers that name.

Resolution order (first match wins, searched from the working directory upward):
`--config PATH`, else `jambu.yaml`, `jambu.yml`, `config.yml`, `config.yaml` in that
order — the shared, namespaced shape is preferred over the legacy flat one when a
directory happens to have both.

`${VAR}` references in string values are expanded from the environment.

Unknown keys are a hard error — a typo in `idle_timeout` must not silently disable
your cost protection. (This applies within the `gpu_runtime:` section; keys other
tools own elsewhere in the document are none of `jambu-gpu`'s business.)

### `model_profile` — don't duplicate model config across files

Set `model_profile: <name>` instead of declaring `model:`/`compute:`/`runtime:`
directly, and jambu-gpu merges in that named entry from `jambu.models.yaml`
(searched the same way as `jambu.yaml` — this directory, then parents, so one
catalog serves every sub-project in a repo). Fields you *do* declare locally
still win over the profile's. See
[`docs/CHOOSING_A_MODEL.md`](CHOOSING_A_MODEL.md#dont-write-a-new-jambuyaml-per-model---use-the-catalog)
for why this exists and `jambu-gpu profiles` / `jambu-gpu inspect-model
--add-profile` to manage the catalog.

---

## `provider`

| Field | Default | Meaning |
| --- | --- | --- |
| `name` | `vast` | Adapter to use. `jambu-gpu providers` lists what is available. |
| `profile` | `default` | Credential profile. Looks up `VAST_API_KEY__<profile>` before `VAST_API_KEY`. |
| `options` | `{}` | Provider-specific tuning, validated by the adapter (see below). |

### `provider.options` for vast

| Field | Default | Meaning |
| --- | --- | --- |
| `api_base` | `https://console.vast.ai/api/v0` | API endpoint. |
| `runtype` | `ssh_direc ssh_proxy` | Vast container run mode. |
| `verified_only` | `true` | Only rent verified machines. |
| `min_reliability` | `0.95` | Minimum machine reliability score. |
| `min_inet_down_mbps` | `100` | Minimum download bandwidth (model pulls are big). |
| `min_cuda` | `12.1` | Minimum `cuda_max_good`. |
| `search_limit` | `24` | How many offers to consider. |
| `order_by` / `order_dir` | `dph_total` / `asc` | Offer ranking — cheapest first. |
| `image_login` | – | `-u user -p pass docker.io` for a private image. |
| `search_filters` | – | Raw extra filters merged into the offer query. |

---

## `compute`

Not sure what to put here for a given model? `jambu-gpu inspect-model <repo>`
reads the model's real HuggingFace metadata and suggests values — see
[`docs/CHOOSING_A_MODEL.md`](CHOOSING_A_MODEL.md) for exactly which source
each number comes from.

| Field | Default | Meaning |
| --- | --- | --- |
| `gpu.min_vram_gb` | `24` | Minimum VRAM **per GPU**. |
| `gpu.count` | `1` | Number of GPUs on one machine. |
| `gpu.name_filter` | – | Substring match, e.g. `A100`, `RTX 4090`. |
| `gpu.cuda_min` | – | Minimum CUDA version. |
| `disk_gb` | `80` | Disk. Must fit the model weights plus the container. |
| `region` | – | Comma-separated geolocations. |
| `spot` | `false` | Use interruptible/bid capacity where supported. |
| `image` | – | Override the runtime's container image. |

---

## `model`

| Field | Default | Meaning |
| --- | --- | --- |
| `id` | *required* | HuggingFace repo id. |
| `revision` | – | Pin a commit/branch/tag. |
| `hf_token_env` | `HF_TOKEN` | Env var holding the HF token for gated models. |
| `trust_remote_code` | `false` | Pass `--trust-remote-code` to the runtime. |
| `served_name` | – | Name the model is served under (`--served-model-name`). |

---

## `runtime`

How the model runs. Independent from where it runs.

| Field | Default | Meaning |
| --- | --- | --- |
| `engine` | `vllm` | `vllm` or `none` (bare compute, start nothing). |
| `port` | `8000` | Container port the server listens on; published publicly. |
| `context_length` | `16384` | `--max-model-len`. |
| `dtype` | `auto` | vLLM dtype. |
| `gpu_memory_utilization` | `0.90` | Fraction of VRAM vLLM may claim. |
| `tensor_parallel_size` | `compute.gpu.count` | Shards across GPUs. |
| `api_key` | – | Require this key on the model endpoint. |
| `vllm_args` | `{}` | Structured escape hatch for any vLLM flag this schema doesn't model: `{flag: value}` → `--flag value` (`true`/`false` → a bare/negated flag, a dict or list → JSON-encoded). Applied before `extra_args`. See [`docs/CHOOSING_A_MODEL.md`](CHOOSING_A_MODEL.md#dynamic--un-modeled-vllm-flags). |
| `extra_args` | `[]` | Raw argv appended verbatim, last — for anything `vllm_args` can't express. |
| `tool_calling` | `auto` | `auto` guesses `--tool-call-parser` from `model.id` and turns tool calling on if recognized; `on` requires a resolvable parser (fails `validate` otherwise); `off` never passes tool-calling flags. |
| `tool_call_parser` | – | Explicit vLLM parser name, overrides the guess. See [vLLM's tool calling docs](https://docs.vllm.ai/en/latest/features/tool_calling/) for the current list (`hermes`, `llama3_json`, `llama4_pythonic`, `mistral`, `granite`, `qwen3_xml`, `deepseek_v3`, ...). |
| `tool_server` | – | MCP server URL (or `demo`) vLLM routes `/v1/responses` tool calls to **server-side**. Leave unset to let the calling agent framework execute tools itself. |

`jambu-gpu inspect-model <repo>` suggests `dtype`, `context_length` and
`tool_calling` for a given model too — see
[`docs/CHOOSING_A_MODEL.md`](CHOOSING_A_MODEL.md).

### Agentic flows: nothing to build

vLLM's OpenAI-compatible server already implements both agent-facing APIs -
`jambu-gpu` only turns on the right flags and hands you the same endpoint it
always has:

- **`/v1/chat/completions`** with `tools=[...]` - works with `tool_calling: auto`
  or an explicit `tool_call_parser`, same as any OpenAI-compatible tool-calling
  setup (LangChain, LangGraph, CrewAI, the raw `openai` SDK, ...).
- **`/v1/responses`** - the OpenAI Responses API, byte-compatible with the
  official `openai` Python/JS SDK's `client.responses.create(...)`. No config
  needed to expose the route; it needs the same tool-call parser as chat
  completions to actually *use* tools, and `tool_server` if you want vLLM to
  execute MCP tools itself instead of your agent framework doing it.

`jambu-gpu run` exports `JAMBU_GPU_ENDPOINT`, `JAMBU_MODEL_ID` and
`OPENAI_API_KEY` - the whole integration surface. Any OpenAI-SDK-compatible
agent framework just works against the instance jambu-gpu provisioned;
[`examples/mastra-agent/`](../examples/mastra-agent/) is a full installable
[Mastra](https://mastra.ai) project built on exactly those three env vars.
`jambu-gpu validate` and `jambu-gpu status` report which parser is active
(`vLLM :8000 (model, tools=hermes)`); if it's missing for a model you expect
to call tools, set `runtime.tool_call_parser` explicitly.

---

## `lifecycle`

The cost protection. See [the safety invariant](#safety-invariant).

| Field | Default | Meaning |
| --- | --- | --- |
| `max_lifetime` | `6h` | Hard ceiling on instance age. `null` = unbounded. |
| `idle_timeout` | `15m` | Shut down after this long with no activity. `null` = unbounded. |
| `auto_stop` | `true` | The guard may stop the instance. |
| `auto_destroy` | `false` | The guard destroys instead of stopping. |
| `cleanup_on_setup_failure` | `true` | Destroy partially created resources when setup fails. |
| `allow_indefinite_lock` | `false` | Permit `jambu-gpu lock` with no TTL. |
| `heartbeat_interval` | `30s` | How often a running workload reports in. |
| `heartbeat_grace` | `5m` | Missing heartbeats tolerated before a workload is stale. |
| `ssh_counts_as_activity` | `false` | An SSH session alone is not useful work. |
| `lock.enabled` | `false` | Whether locks are offered in this project. |
| `lock.default_ttl` | `2h` | TTL used by `jambu-gpu lock` with no `--for`. |
| `watchdog.enabled` | `true` | Install the independent remote guard. |
| `watchdog.port` | `8777` | Control port published on the instance. |
| `watchdog.interval` | `60s` | How often the remote guard evaluates the policy. |

At least one of `idle_timeout` / `max_lifetime` **must** be set — `validate` fails
otherwise.

`idle_timeout` may not exceed `max_lifetime`.

---

## `health`

| Field | Default | Meaning |
| --- | --- | --- |
| `interval` | `60s` | Probe interval while waiting for the runtime. |
| `startup_timeout` | `10m` | Give up if the model server never answers. Raise it for very large models. |
| `path` | `/health` | Health path on the model endpoint. |

A passing health check is **not** activity. A healthy but unused server still goes idle.

---

## `budget`

| Field | Default | Meaning |
| --- | --- | --- |
| `max_hourly_cost_usd` | – | Refuse to provision above this hourly price. |
| `max_session_cost_usd` | – | The remote guard shuts down once accrued cost passes this. |

Override once with `jambu-gpu setup --allow-cost-override`.

---

## `workload`

| Field | Default | Meaning |
| --- | --- | --- |
| `mode` | `local` | `local` runs your command on your machine against the remote endpoint; `remote` runs it on the instance over SSH. |
| `env` | `{}` | Extra environment variables for the workload. |
| `workdir` | project dir | Working directory for the command. |

---

## Safety invariant

An instance may remain billable only while useful work is running, useful work is
queued or imminent within the idle window, or the user explicitly locked it.
Everything else ends in `STOPPED` — or `DESTROYED` where stopping does not end
billing.

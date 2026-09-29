# How shutdown works

The CLI process cannot be responsible for idle shutdown. Close the laptop and a local watchdog dies while the GPU keeps billing. `setup` installs a watchdog on the instance itself:

```
CLI ──provisions──> instance
                      │
                      ├── watchdog (HTTP :8777, authenticated)
                      │     ├── holds the activity and heartbeat state
                      │     ├── evaluates the lifecycle policy every 60s
                      │     └── calls the provider API to stop or destroy itself
                      └── vLLM :8000
```

The watchdog script is generated at provision time. The lifecycle policy source is inlined from `jambu_gpu/core/policy.py`, so the remote guard and `gpu status` apply the same rule. The provider adapter injects only the two calls that stop or destroy the machine.

An instance stays billable only while:

1. a workload is running (heartbeat alive), or
2. a workload is queued, or the model is still starting, or
3. an explicit lock is active.

Anything else becomes `STOPPED` or `DESTROYED`, with the reason recorded.

Activity is a running workload, a queued workload, model startup, or an active lock. A healthy vLLM server, a health check, and an open SSH session are not activity. If they were, an idle server would stay up forever.

A crashed CLI, a dropped SSH session, or a sleeping laptop stops the heartbeat. After `lifecycle.heartbeat_grace` the workload is stale, the instance goes idle, and the normal idle rules apply.

## A second path

Run the same rule from anywhere that has API access:

```bash
*/5 * * * * cd /path/to/experiment && gpu guard
```

`guard` evaluates the policy against reconciled state and enforces it.

## The watchdog credential

The watchdog stops its own machine, so it needs provider API access. `VAST_API_KEY` is written on the instance at `/etc/jambu/watchdog.json` (mode 600).

Use a dedicated, restricted Vast.ai key for this, not a personal key. Set `lifecycle.watchdog.enabled: false` to opt out. Shutdown then depends on `gpu guard` or on the CLI staying alive, and `validate` warns you.

## Architecture

```
jambu.yaml (gpu_runtime:)
    │
    ▼
gpu
    │
    ├── Lifecycle    core/policy.py     the only shutdown rule
    ├── Runtime      runtime/vllm.py    how the model runs
    └── State        core/state.py      .jambu/state.json
                │
        Provider contract   providers/base.py
                │
        ┌───────┴────────┐
        ▼                ▼
    Vast adapter    future adapter
```

Four concerns stay independent: model, inference runtime, lifecycle policy, infrastructure provider. The core never branches on a provider name. It branches on `provider.capabilities`.

```
jambu_gpu/
├── cli/          setup, run, status, lifecycle, providers, validate
├── core/         config, models, policy, state, events, engine, execution, health
├── providers/    base, registry, vast/
├── runtime/      base, vllm
└── watchdog/     builder, agent_body
```

Adding a provider: [ADDING_A_PROVIDER.md](ADDING_A_PROVIDER.md). The guarantees and the tests that hold them: [ACCEPTANCE.md](ACCEPTANCE.md).

# GPU Runtime CLI

## 1. Objective

Create a provider-agnostic CLI for provisioning and managing temporary GPU runtimes used by Jambu.ai Lab projects.

The CLI must:

- provision GPU infrastructure from a selected provider;
- deploy and expose a requested model/runtime;
- execute workloads against that runtime;
- control instance lifecycle automatically;
- prevent idle GPU instances from accumulating cost;
- provide a fixed provider contract;
- keep provider-specific implementation isolated;
- use `config.yml` as the declarative Source of Truth.

Initial provider:

`vast.ai`

The architecture must allow additional providers without changes to the core orchestration logic.

---

# 2. Core Principle

The system separates:

```text
WHAT should run
        │
        ▼
   config.yml
        │
        ▼
GPU Runtime CLI
        │
        ▼
Provider Contract
        │
   ┌────┴────┐
   ▼         ▼
Vast.ai    Future Provider
Adapter      Adapter
```

The core must never contain Vast.ai-specific logic.

Provider adapters translate the stable internal contract into provider-specific operations.

---

# 3. CLI

Working name:

```bash
gpu
```

Core commands:

```bash
gpu providers
gpu validate
gpu setup
gpu run
gpu status
gpu lock
gpu unlock
gpu stop
gpu destroy
```

Examples:

```bash
gpu setup
```

Reads `config.yml`, selects the configured provider, provisions the instance, prepares the runtime and starts the configured model.

```bash
gpu run experiments/run.py
```

Guarantees the configured runtime is available before executing the workload.

```bash
gpu status
```

Returns normalized provider-independent state.

```bash
gpu stop
```

Stops the compute resource when supported.

```bash
gpu destroy
```

Destroys the remote resource.

---

# 4. Configuration

Example:

```yaml
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

  lock:
    enabled: false

health:
  interval: 60s
  startup_timeout: 10m
```

`config.yml` describes desired behavior.

It MUST NOT contain provider credentials.

---

# 5. Credentials

Credentials are resolved separately from project configuration.

Example:

```bash
VAST_API_KEY=...
```

Each provider declares its requirements.

Example provider metadata:

```python
class VastProvider(GPUProvider):
    name = "vast"

    required_credentials = [
        "VAST_API_KEY",
    ]
```

Validation must fail before provisioning when required credentials are unavailable.

Future implementations may support:

```text
environment variables
system keychain
secret managers
CI/CD secrets
```

without changing `config.yml`.

---

# 6. Provider Contract

Every provider MUST implement the same contract.

```python
class GPUProvider(ABC):

    @abstractmethod
    def validate(self, config: RuntimeConfig) -> ValidationResult:
        ...

    @abstractmethod
    def setup(self, spec: ComputeSpec) -> Instance:
        ...

    @abstractmethod
    def start(self, instance: Instance) -> Instance:
        ...

    @abstractmethod
    def run(
        self,
        instance: Instance,
        workload: Workload,
    ) -> Execution:
        ...

    @abstractmethod
    def status(self, instance: Instance) -> InstanceStatus:
        ...

    @abstractmethod
    def stop(self, instance: Instance) -> None:
        ...

    @abstractmethod
    def destroy(self, instance: Instance) -> None:
        ...

    @abstractmethod
    def logs(self, instance: Instance) -> LogStream:
        ...
```

Provider-specific APIs, SDKs, instance identifiers, pricing structures and authentication MUST NOT leak through this interface.

---

# 7. Provider Capabilities

Not every GPU provider behaves identically.

Therefore adapters must explicitly expose capabilities.

Example:

```python
@dataclass
class ProviderCapabilities:
    stop: bool
    destroy: bool
    persistent_disk: bool
    spot_instances: bool
    ssh: bool
    public_ports: bool
    gpu_selection: bool
    pricing_query: bool
```

Example:

```bash
gpu providers
```

could return:

```text
PROVIDER    STATUS       GPU     STOP    DESTROY    PRICING
vast        configured   yes     yes     yes        yes
runpod      unavailable  yes     yes     yes        yes
```

The core must make decisions based on capabilities rather than provider names.

Never:

```python
if provider == "vast":
    ...
```

Instead:

```python
if provider.capabilities.stop:
    provider.stop(instance)
```

---

# 8. Normalized Runtime State

Providers return normalized state:

```python
class InstanceState(Enum):
    PROVISIONING = "provisioning"
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    IDLE = "idle"
    STOPPED = "stopped"
    FAILED = "failed"
    DESTROYED = "destroyed"
```

Provider-specific states are mapped internally by the adapter.

---

# 9. Runtime State

Operational state is stored separately from `config.yml`.

Example:

```text
.jambu/
    state.json
    runtime.lock
    logs/
```

Example:

```json
{
  "provider": "vast",
  "instance_id": "19384723",
  "created_at": "2026-09-24T16:00:00Z",
  "last_activity": "2026-09-24T17:12:31Z",
  "state": "idle",
  "locked": false
}
```

`config.yml` remains the desired-state SoT.

`state.json` represents observed runtime state.

---

# 10. Lifecycle Guard

A lifecycle controller MUST prevent abandoned GPU resources.

Core rule:

```text
IF
    instance is running
AND no workload is running
AND no workload is queued
AND instance is not explicitly locked
AND idle_timeout has expired

THEN
    stop instance
```

Additionally:

```text
IF
    instance lifetime > max_lifetime
AND instance is not locked

THEN
    stop instance
```

This rule applies regardless of provider.

---

# 11. Lock

Users can explicitly prevent automatic shutdown.

```bash
gpu lock
```

Optional TTL:

```bash
gpu lock --for 2h
```

State:

```yaml
locked: true
locked_until: "2026-09-24T20:00:00Z"
```

After TTL expiration, normal lifecycle rules resume.

Permanent locks should require explicit configuration:

```yaml
lifecycle:
  allow_indefinite_lock: false
```

Default:

```text
false
```

This prevents forgotten manual locks from defeating cost protection.

---

# 12. Activity Definition

A resource MUST NOT be considered active merely because the VM is running.

Activity means one of:

```text
workload currently executing
workload queued for execution
model startup in progress
explicit user lock active
```

SSH connection alone should NOT automatically count as activity unless configured.

Model server health checks also do NOT count as workload activity.

Otherwise a healthy but unused vLLM server would keep the GPU alive forever.

---

# 13. Heartbeat

Long-running workloads must maintain a heartbeat.

Example:

```text
Execution
   │
   ├── heartbeat
   ├── heartbeat
   ├── heartbeat
   └── complete
```

If heartbeat disappears unexpectedly:

```text
execution → UNKNOWN
```

After a configurable grace period:

```text
UNKNOWN → IDLE
```

Normal idle shutdown rules then apply.

This protects against:

```text
CLI crash
SSH disconnect
experiment crash
local machine sleep
lost network connection
```

---

# 14. Lifecycle Enforcement

The CLI process itself cannot be responsible for idle shutdown.

If the developer closes the laptop, a local watchdog disappears while the expensive GPU continues running.

Therefore lifecycle enforcement MUST have an independent execution path.

Preferred architecture:

```text
CLI
 │
 ├── provisions instance
 │
 └── installs lifecycle watchdog
              │
              ▼
        Remote Instance
              │
        checks activity
              │
        provider API
              │
              ▼
           STOP
```

Alternative:

```text
scheduled external controller
```

The lifecycle policy must survive termination of the local CLI.

---

# 15. Provisioning Flow

```text
gpu setup
       │
       ▼
Load config.yml
       │
       ▼
Validate schema
       │
       ▼
Resolve provider
       │
       ▼
Validate credentials
       │
       ▼
Validate capabilities
       │
       ▼
Find suitable GPU
       │
       ▼
Provision
       │
       ▼
Install runtime
       │
       ▼
Download model
       │
       ▼
Start model server
       │
       ▼
Health check
       │
       ▼
READY
```

Any failure must trigger cleanup according to policy.

Example:

```yaml
lifecycle:
  cleanup_on_setup_failure: true
```

Default should be `true`.

---

# 16. Run Flow

```bash
gpu run python experiments/qwythos_eval.py
```

Flow:

```text
load configuration
        ↓
resolve current instance
        ↓
instance exists?
    ┌───┴───┐
   NO      YES
    │        │
 setup    stopped?
             │
            start
             │
             ▼
        wait READY
             │
             ▼
      register workload
             │
             ▼
          execute
             │
             ▼
      heartbeat activity
             │
             ▼
      record completion
             │
             ▼
          IDLE
```

The instance is NOT immediately stopped after every command.

The configured idle timeout determines reuse.

This avoids repeated provisioning when running multiple experiments sequentially.

---

# 17. Cost Guardrails

Required guardrails:

```text
idle_timeout
max_lifetime
setup failure cleanup
expired lock detection
orphan workload detection
provider reconciliation
```

Optional:

```yaml
budget:
  max_hourly_cost_usd: 1.00
  max_session_cost_usd: 5.00
```

If the provider supports pricing information, provisioning exceeding the configured threshold MUST fail unless explicitly overridden.

Example:

```bash
gpu setup --allow-cost-override
```

---

# 18. Reconciliation

Local state cannot be trusted as authoritative about remote infrastructure.

Therefore:

```bash
gpu status
```

must reconcile:

```text
local state
        +
provider state
        =
normalized observed state
```

Example:

```text
state.json says RUNNING
provider says instance missing
```

Result:

```text
DESTROYED
```

and local state is repaired.

This prevents stale state after manual provider operations.

---

# 19. Provider Structure

Suggested package structure:

```text
jambu_gpu/
├── cli/
│   ├── setup.py
│   ├── run.py
│   ├── status.py
│   └── lifecycle.py
│
├── core/
│   ├── config.py
│   ├── models.py
│   ├── lifecycle.py
│   ├── execution.py
│   └── state.py
│
├── providers/
│   ├── base.py
│   ├── registry.py
│   │
│   └── vast/
│       ├── provider.py
│       ├── client.py
│       ├── config.py
│       └── mapper.py
│
└── runtime/
    ├── base.py
    └── vllm.py
```

Important separation:

```text
Provider != Runtime
```

Vast.ai answers:

> Where does the compute run?

vLLM answers:

> How does the model run?

Qwythos answers:

> What model runs?

These dimensions must remain independent.

---

# 20. Provider Registration

Providers should register through a registry:

```python
PROVIDERS = {
    "vast": VastProvider,
}
```

Resolution:

```python
provider = registry.get(config.provider.name)
```

Adding another provider should require only:

```text
providers/<provider>/
```

plus registration.

No modification to lifecycle, execution or CLI logic should be required.

---

# 21. Provider Adapter Responsibilities

Each provider implementation owns:

```text
authentication
credential declaration
API communication
GPU discovery
pricing discovery
instance creation
instance startup
instance shutdown
instance destruction
network configuration
SSH configuration
provider state mapping
provider error translation
```

It does NOT own:

```text
idle policy
budget policy
workload semantics
model configuration
runtime selection
project configuration
```

Those belong to the core.

---

# 22. Error Model

Provider errors must be normalized.

```python
class ProviderError(Exception):
    pass

class AuthenticationError(ProviderError):
    pass

class CapacityUnavailableError(ProviderError):
    pass

class ProvisioningError(ProviderError):
    pass

class ProviderTimeoutError(ProviderError):
    pass

class UnsupportedCapabilityError(ProviderError):
    pass
```

CLI behavior must therefore remain identical across providers.

---

# 23. Idempotency

Commands must be idempotent wherever practical.

Running:

```bash
gpu setup
```

twice MUST NOT create two instances.

Expected behavior:

```text
existing compatible instance → reuse
existing incompatible instance → explicit error/recreate decision
no instance → provision
```

Likewise:

```bash
gpu stop
```

on an already stopped instance succeeds safely.

---

# 24. Observability

Every lifecycle action should produce structured events.

Example:

```json
{
  "event": "instance.stop",
  "reason": "idle_timeout",
  "provider": "vast",
  "instance_id": "19384723",
  "idle_seconds": 934
}
```

Minimum events:

```text
provision_requested
instance_created
runtime_starting
runtime_ready
workload_started
workload_finished
instance_idle
instance_locked
instance_unlocked
instance_stopped
instance_destroyed
lifecycle_guard_triggered
provider_error
```

This becomes essential when investigating unexpected GPU costs.

---

# 25. Safety Invariant

The fundamental lifecycle invariant is:

```text
A GPU instance may remain billable only when:

1. useful work is running;
2. useful work is queued/imminent within the configured idle window; or
3. the user explicitly requested it to remain available.
```

Anything else eventually transitions to:

```text
STOPPED
```

or, where stop does not eliminate billing:

```text
DESTROYED
```

The adapter must expose enough billing semantics for the core to distinguish those cases.

---

# 26. MVP

### Phase 1

Implement only:

```text
config.yml schema
CLI
provider contract
provider registry
Vast.ai adapter
vLLM runtime
setup
run
status
stop
destroy
lock/unlock
idle timeout
max lifetime
remote watchdog
structured logs
```

Do NOT initially implement:

```text
multi-instance scheduling
multi-GPU orchestration
Kubernetes
distributed inference
provider optimization
automatic provider selection
job queues
web dashboard
```

Those are separate problems.

---

# 27. Acceptance Criteria

The MVP is complete when this works:

```bash
git clone <experiment>

cd experiment

export VAST_API_KEY=...

gpu validate

gpu setup

gpu run python experiment.py

gpu status
```

and the following guarantees hold:

1. Repeated `setup` does not accidentally create duplicate infrastructure.
2. Provider-specific APIs do not leak into core orchestration.
3. Missing credentials fail before provisioning.
4. Failed provisioning cleans up partially created resources.
5. Active workloads are never intentionally stopped by the idle guard.
6. An idle unlocked instance is automatically stopped within `idle_timeout + watchdog_interval`.
7. `max_lifetime` prevents forgotten instances from remaining active indefinitely.
8. Explicit locks prevent automatic shutdown until expiration.
9. A crashed/disconnected CLI does not disable lifecycle protection.
10. Remote state is reconciled with provider state.
11. Adding a provider requires implementing the provider contract, not modifying lifecycle logic.
12. Every automatic shutdown records the reason that triggered it.

## Final Architecture

```text
                 config.yml
                     │
                     ▼
               Jambu GPU CLI
                     │
          ┌──────────┼──────────┐
          ▼          ▼          ▼
      Lifecycle    Runtime     State
      Controller   Manager     Manager
          │          │
          │        vLLM
          │          │
          └────┬─────┘
               ▼
        Provider Contract
               │
       ┌───────┼────────┐
       ▼       ▼        ▼
     Vast    RunPod    Future
       │
       ▼
      GPU
```

The contract boundary is the critical part of the design: **model, inference runtime, lifecycle policy and infrastructure provider remain four independent concerns**. This keeps the CLI useful beyond Qwythos and prevents the first Vast.ai implementation from defining the architecture of the system.
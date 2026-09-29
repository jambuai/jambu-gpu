# Acceptance criteria — where each one is implemented and tested

Twelve guarantees, mapped to the code and the test that holds each one.

| # | Guarantee | Implementation | Test |
| --- | --- | --- | --- |
| 1 | Repeated `setup` does not duplicate infrastructure | `core/engine.py::Engine.setup` reconciles first, compares `config.fingerprint()`, and holds an exclusive `.jambu/runtime.lock` | `test_engine.py::test_setup_twice_does_not_create_two_instances`, `::test_setup_refuses_to_reuse_an_incompatible_instance` |
| 2 | Provider APIs do not leak into core orchestration | `providers/base.py` contract; all Vast wire format confined to `providers/vast/{client,mapper}.py` | `test_engine.py::test_the_core_never_branches_on_a_provider_name` (greps `core/`) |
| 3 | Missing credentials fail before provisioning | `VastProvider.validate` returns before any API call; `CredentialResolver` never reads jambu.yaml | `test_vast_provider.py::test_validate_fails_before_provisioning_without_a_key` (asserts zero HTTP calls) |
| 4 | Failed provisioning cleans up | `Engine.setup` persists state *before* the long wait, then destroys on failure when `cleanup_on_setup_failure` | `test_engine.py::test_failed_setup_cleans_up_partially_created_resources` |
| 5 | Active workloads are never stopped by the idle guard | `core/policy.py::evaluate` returns `workload_active` before any idle check | `test_policy.py::test_active_workload_is_never_stopped_by_the_idle_guard`, `test_watchdog.py::test_a_heartbeating_workload_is_never_stopped` |
| 6 | An idle unlocked instance stops within `idle_timeout + watchdog_interval` | remote watchdog loop, `watchdog/agent_body.py::guard_loop` | `test_watchdog.py::test_idle_instance_is_stopped_with_a_recorded_reason` (real subprocess) |
| 7 | `max_lifetime` bounds forgotten instances | `policy.evaluate` checks age before activity | `test_policy.py::test_max_lifetime_stops_even_when_busy`, `test_watchdog.py::test_max_lifetime_stops_a_busy_instance` |
| 8 | Locks prevent shutdown until expiry | `policy.Snapshot.lock_active` / `lock_expired`; TTL required unless `allow_indefinite_lock` | `test_policy.py::test_expired_lock_stops_protecting`, `test_watchdog.py::test_a_lock_prevents_shutdown_until_it_expires` |
| 9 | A crashed CLI does not disable protection | the guard runs *on the instance*; heartbeats go stale after `heartbeat_grace` | `test_watchdog.py::test_a_vanished_heartbeat_falls_back_to_idle`, `::test_startup_grace_expires_when_the_cli_never_confirms` |
| 10 | Remote state is reconciled | `Engine._reconcile` merges provider truth + watchdog truth and repairs local state | `test_engine.py::test_status_repairs_state_when_the_instance_vanished` |
| 11 | A new provider needs only the contract | `providers/registry.py`; the engine is driven end to end by `tests/fakes.py::FakeProvider` | `test_engine.py::test_a_new_provider_needs_only_the_contract` |
| 12 | Every automatic shutdown records its reason | `lifecycle_guard_triggered` + `instance_stopped` events carry `reason`, locally and on the instance | `test_engine.py::test_guard_stops_an_idle_unlocked_instance_and_records_the_reason`, `test_watchdog.py::test_idle_instance_is_stopped_with_a_recorded_reason` |

## The MVP command surface

`jambu.yaml` schema, CLI, provider contract, provider registry, Vast.ai adapter,
vLLM runtime, `setup`, `run`, `status`, `stop`, `destroy`, `lock`/`unlock`, idle
timeout, max lifetime, remote watchdog, structured logs — all present.

Deliberately not implemented: multi-instance scheduling, multi-GPU
orchestration, Kubernetes, distributed inference, provider optimization, automatic
provider selection, job queues, web dashboard.

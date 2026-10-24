# Acceptance criteria — where each one is implemented and tested

Sixteen guarantees, mapped to the code and the test that holds each one.

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
| 13 | A restarted instance stays guarded | `Engine._reconcile` takes running/stopped from the provider; the watchdog's `terminated` flag only counts when the provider cannot tell. `RuntimeState.session_started_at` restarts on every start, and `max_lifetime` and the session budget are measured from it | `test_engine_sessions.py::test_reconcile_trusts_the_provider_over_a_stale_terminated_flag`, `::test_a_stale_terminated_flag_does_not_disable_the_guard`, `::test_restarting_a_stopped_instance_starts_a_new_session`, `::test_guard_budget_resets_with_a_new_session`; watchdog-side session tests in `test_policy.py` / `test_watchdog.py` |
| 14 | Traffic on the model endpoint is activity | `VllmRuntime.activity_probe` hands the watchdog vLLM's `/metrics` gauges and counters; `lifecycle.traffic_counts_as_activity` turns it off | `test_engine_sessions.py::test_the_watchdog_is_told_how_to_read_vllm_traffic`, `::test_traffic_can_be_excluded_from_activity`, `test_runtime_vllm.py::test_activity_probe_reads_vllm_metrics_on_loopback`; the watchdog's metrics-reading tests in `test_watchdog.py` |
| 15 | A stopped instance does not bill storage forever | `lifecycle.stopped_ttl` -> `PolicyConfig.stopped_ttl_s`; `RuntimeState.stopped_since`; `Engine.guard_tick` destroys with reason `stopped_ttl`; `validate` warns when nothing would remove it | `test_engine_sessions.py::test_guard_destroys_an_instance_stopped_longer_than_stopped_ttl`, `::test_validate_warns_when_nothing_removes_a_billing_stopped_instance`; the stopped-TTL policy tests in `test_policy.py` |
| 16 | Orphans are found account-wide, and nothing else is touched | `GPUProvider.list_instances` (capability `list_instances`) returns only `jambu-` labels; `core/sweep.py` applies `max_lifetime + 30m` and `stopped_ttl`, keeps first-seen-stopped times in `~/.jambu/sweep.json`, and filters labels again itself; `gpu sweep --install-timer` schedules it | `test_sweep.py::test_vast_lists_only_jambu_labelled_instances`, `::test_non_jambu_instances_are_never_touched`, `::test_a_running_orphan_past_max_lifetime_and_grace_is_destroyed`, `::test_a_stopped_instance_is_destroyed_once_seen_stopped_past_stopped_ttl`, `::test_install_timer_writes_user_units_for_sweep_and_project_guard` |

## The MVP command surface

`jambu.yaml` schema, CLI, provider contract, provider registry, Vast.ai adapter,
vLLM runtime, `setup`, `run`, `status`, `stop`, `destroy`, `lock`/`unlock`, idle
timeout, max lifetime, remote watchdog, structured logs — all present. On top of
it, `ls` and `sweep` for account-wide orphan control.

Deliberately not implemented: multi-instance scheduling, multi-GPU
orchestration, Kubernetes, distributed inference, provider optimization, automatic
provider selection, job queues, web dashboard.

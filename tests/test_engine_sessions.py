"""Engine: restarts, sessions, stopped_ttl, traffic as activity, session budget."""

from __future__ import annotations

import pytest
from fakes import FakeProvider

from jambu_gpu.core.credentials import CredentialResolver
from jambu_gpu.core.engine import Engine
from jambu_gpu.core.models import InstanceState, ProviderCapabilities
from jambu_gpu.core.state import utc_now


def make_engine(config):
    provider = FakeProvider(config, CredentialResolver(config.project_dir))
    return Engine(config, provider=provider)


@pytest.fixture
def engine(config):
    return make_engine(config)


# -- reconcile vs. a stale watchdog `terminated` flag -----------------------


class _StaleWatchdog:
    """A watchdog that still remembers stopping the machine before a restart."""

    def try_state(self):
        return {"terminated": True, "locked": False, "starting": False}

    def events(self, limit: int = 100):
        return []


def test_reconcile_trusts_the_provider_over_a_stale_terminated_flag(engine, monkeypatch):
    state = engine.setup(wait=False)
    monkeypatch.setattr(engine, "beacon", lambda _state: _StaleWatchdog())

    reconciled = engine.reconcile()
    assert reconciled.state != "stopped"
    assert reconciled.running_since is not None  # still billing, so still guarded
    assert reconciled.stopped_since is None
    assert engine.provider.instances[state.instance_id].state is InstanceState.READY


def test_a_stale_terminated_flag_does_not_disable_the_guard(write_config, monkeypatch):
    engine = make_engine(write_config(lifecycle={"idle_timeout": "1m"}))
    engine.setup(wait=False)
    monkeypatch.setattr(engine, "beacon", lambda _state: _StaleWatchdog())
    with engine.store.transaction() as stored:
        stored.last_activity = utc_now() - 3600
        stored.starting = False

    decision = engine.guard_tick(apply=False)
    assert decision["action"] == "stop"
    assert decision["reason"] == "idle_timeout"


def test_terminated_only_counts_when_the_provider_cannot_tell(engine, monkeypatch):
    state = engine.setup(wait=False)
    monkeypatch.setattr(engine, "beacon", lambda _state: _StaleWatchdog())
    engine.provider.instances[state.instance_id].state = InstanceState.UNKNOWN

    assert engine.reconcile().state == "stopped"


def test_a_restart_outside_the_cli_is_seen_as_running_again(engine):
    state = engine.setup(wait=False)
    engine.stop()
    assert engine.store.load().state == "stopped"

    engine.provider.instances[state.instance_id].state = InstanceState.READY  # web console
    before = utc_now()
    reconciled = engine.reconcile()
    assert reconciled.state in ("ready", "idle")
    assert reconciled.stopped_since is None
    assert reconciled.session_started_at >= before


# -- sessions ---------------------------------------------------------------


def test_setup_starts_a_session(engine):
    state = engine.setup(wait=False)
    stored = engine.store.load()
    assert stored.session_started_at == pytest.approx(state.created_at, abs=1)
    assert stored.stopped_since is None


def test_stopping_records_stopped_since_and_destroy_clears_it(engine):
    engine.setup(wait=False)
    engine.stop()
    first = engine.store.load().stopped_since
    assert first is not None

    # A later reconcile keeps the first observation, not the latest.
    assert engine.reconcile().stopped_since == first

    engine.destroy()
    stored = engine.store.load()
    assert stored.stopped_since is None and stored.session_started_at is None


def test_a_stop_observed_by_reconcile_sets_stopped_since(engine):
    state = engine.setup(wait=False)
    engine.provider.instances[state.instance_id].state = InstanceState.STOPPED  # the watchdog did it
    assert engine.reconcile().stopped_since is not None


def test_restarting_a_stopped_instance_starts_a_new_session(engine, monkeypatch):
    engine.setup(wait=False)
    engine.stop()
    with engine.store.transaction() as stored:
        stored.session_started_at = utc_now() - 7 * 3600

    monkeypatch.setattr(engine, "_wait_running", lambda state: state.instance)
    monkeypatch.setattr(engine, "_connect_watchdog", lambda state: None)
    monkeypatch.setattr(engine, "_wait_runtime", lambda state: None)
    before = utc_now()
    state = engine.ensure_ready()

    assert ("start", state.instance_id) in engine.provider.calls
    assert state.session_started_at >= before
    assert state.stopped_since is None
    assert engine.store.load().session_started_at == pytest.approx(state.session_started_at)


def test_snapshot_carries_session_and_stop_times(engine):
    engine.setup(wait=False)
    engine.stop()
    stored = engine.store.load()
    snap = engine.snapshot(stored)
    assert snap.session_started_at == stored.session_started_at
    assert snap.stopped_since == stored.stopped_since


def test_a_restarted_instance_is_not_killed_for_its_age_before_the_restart(write_config):
    engine = make_engine(write_config(lifecycle={"max_lifetime": "6h", "idle_timeout": "15m"}))
    state = engine.setup(wait=False)
    engine.provider.instances[state.instance_id].created_at = utc_now() - 10 * 3600
    with engine.store.transaction() as stored:
        stored.session_started_at = utc_now() - 600  # restarted ten minutes ago
        stored.last_activity = utc_now()
        stored.starting = False

    assert engine.guard_tick()["action"] == "none"


def test_status_reports_session_fields(engine):
    engine.setup(wait=False)
    payload = engine.status()
    assert payload["session_started_at"] is not None
    assert payload["stopped_since"] is None


# -- stopped_ttl --------------------------------------------------------------


def test_policy_config_carries_stopped_ttl(write_config):
    assert make_engine(write_config()).policy_config().stopped_ttl_s == 24 * 3600
    disabled = make_engine(write_config(lifecycle={"stopped_ttl": None}))
    assert disabled.policy_config().stopped_ttl_s is None


def test_guard_destroys_an_instance_stopped_longer_than_stopped_ttl(write_config):
    engine = make_engine(write_config(lifecycle={"stopped_ttl": "1h"}))
    state = engine.setup(wait=False)
    engine.stop()
    with engine.store.transaction() as stored:
        stored.stopped_since = utc_now() - 2 * 3600

    decision = engine.guard_tick()
    assert decision["action"] == "destroy"
    assert decision["reason"] == "stopped_ttl"
    assert ("destroy", state.instance_id) in engine.provider.calls
    assert engine.store.load().instance_id is None
    events = engine.events.tail(limit=50)
    triggered = [r for r in events if r["event"] == "lifecycle_guard_triggered"]
    destroyed = [r for r in events if r["event"] == "instance_destroyed"]
    assert triggered[-1]["reason"] == "stopped_ttl"
    assert destroyed[-1]["reason"] == "stopped_ttl"


def test_guard_leaves_a_recently_stopped_instance_alone(write_config):
    engine = make_engine(write_config(lifecycle={"stopped_ttl": "1h"}))
    engine.setup(wait=False)
    engine.stop()
    assert engine.guard_tick()["action"] == "none"
    assert engine.store.load().instance_id is not None


def test_dry_run_guard_reports_stopped_ttl_without_destroying(write_config):
    engine = make_engine(write_config(lifecycle={"stopped_ttl": "1h"}))
    engine.setup(wait=False)
    engine.stop()
    with engine.store.transaction() as stored:
        stored.stopped_since = utc_now() - 2 * 3600
    assert engine.guard_tick(apply=False)["action"] == "destroy"
    assert not [c for c in engine.provider.calls if c[0] == "destroy"]


def test_validate_warns_when_nothing_removes_a_billing_stopped_instance(write_config):
    billing_stop = ProviderCapabilities(stop=True, destroy=True, stop_eliminates_billing=False)

    engine = make_engine(write_config(lifecycle={"stopped_ttl": None, "auto_destroy": False}))
    engine.provider.capabilities = billing_stop
    result = engine.validate(check_provider=False)
    assert any(issue.field == "lifecycle.stopped_ttl" for issue in result.warnings)

    covered = make_engine(write_config(lifecycle={"stopped_ttl": "24h"}))
    covered.provider.capabilities = billing_stop
    result = covered.validate(check_provider=False)
    assert not any(issue.field == "lifecycle.stopped_ttl" for issue in result.warnings)

    # Where stopping ends billing, a kept stopped instance costs nothing.
    free_stop = make_engine(write_config(lifecycle={"stopped_ttl": None}))
    result = free_stop.validate(check_provider=False)
    assert not any(issue.field == "lifecycle.stopped_ttl" for issue in result.warnings)


# -- activity probe -----------------------------------------------------------


def _captured_agent_config(engine, monkeypatch):
    import jambu_gpu.core.engine as engine_module

    captured = {}
    real = engine_module.build_agent_config

    def _capture(**kwargs):
        captured.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(engine_module, "build_agent_config", _capture)
    engine._build_onstart("token")
    return captured


def test_the_watchdog_is_told_how_to_read_vllm_traffic(engine, monkeypatch):
    captured = _captured_agent_config(engine, monkeypatch)
    assert captured["activity_probe"] == {
        "kind": "prometheus",
        "url": "http://127.0.0.1:8000/metrics",
        "gauges": ["vllm:num_requests_running", "vllm:num_requests_waiting"],
        "counters": ["vllm:request_success_total", "vllm:prompt_tokens_total"],
    }


def test_traffic_can_be_excluded_from_activity(write_config, monkeypatch):
    engine = make_engine(write_config(lifecycle={"traffic_counts_as_activity": False}))
    assert _captured_agent_config(engine, monkeypatch)["activity_probe"] is None


def test_bare_compute_has_no_activity_probe(write_config, monkeypatch):
    engine = make_engine(write_config(runtime={"engine": "none"}))
    assert _captured_agent_config(engine, monkeypatch)["activity_probe"] is None


def test_the_watchdog_policy_carries_stopped_ttl(engine, monkeypatch):
    captured = _captured_agent_config(engine, monkeypatch)
    assert captured["policy"].stopped_ttl_s == 24 * 3600


# -- the guard's budget is per session and uses the hourly price -------------


def _budget_engine(write_config):
    engine = make_engine(
        write_config(
            lifecycle={"max_lifetime": "12h", "idle_timeout": "15m"},
            budget={"max_session_cost_usd": 2.0},
        )
    )
    engine.provider.offer_price = 1.0
    return engine


def test_guard_budget_uses_the_session_age_and_hourly_price(write_config):
    engine = _budget_engine(write_config)
    engine.setup(wait=False)
    with engine.store.transaction() as stored:
        stored.session_started_at = utc_now() - 3 * 3600  # $3 at $1/h
        stored.last_activity = utc_now()
        stored.starting = False

    decision = engine.guard_tick(apply=False)
    assert decision["action"] == "stop"
    assert decision["reason"] == "budget_exceeded"
    assert decision["detail"]["session_cost_usd"] == pytest.approx(3.0, abs=0.01)
    assert engine.policy_config(1.0).hourly_cost_usd == 1.0


def test_guard_budget_resets_with_a_new_session(write_config):
    engine = _budget_engine(write_config)
    state = engine.setup(wait=False)
    engine.provider.instances[state.instance_id].created_at = utc_now() - 5 * 3600
    with engine.store.transaction() as stored:
        stored.session_started_at = utc_now() - 1800  # restarted: $0.50 so far
        stored.last_activity = utc_now()
        stored.starting = False

    assert engine.guard_tick(apply=False)["action"] == "none"


def test_a_state_file_from_before_sessions_counts_from_creation(engine):
    engine.setup(wait=False)
    with engine.store.transaction() as stored:
        stored.session_started_at = None
        created = stored.created_at
    assert engine.reconcile().session_started_at == pytest.approx(created, abs=1)

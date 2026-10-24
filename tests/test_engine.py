"""Orchestration: idempotency, reconciliation, cleanup, budget, the guard."""

from __future__ import annotations

import pytest
from fakes import FakeProvider

from jambu_gpu.core.credentials import CredentialResolver
from jambu_gpu.core.engine import Engine
from jambu_gpu.core.errors import BudgetExceededError, ConfigError, ProvisioningError
from jambu_gpu.core.models import InstanceState
from jambu_gpu.core.state import utc_now


@pytest.fixture
def engine(config):
    provider = FakeProvider(config, CredentialResolver(config.project_dir))
    return Engine(config, provider=provider)


def make_engine(config):
    provider = FakeProvider(config, CredentialResolver(config.project_dir))
    return Engine(config, provider=provider)


# Without a watchdog the CLI guard owns the idle rule; with one it defers to it
# (see test_engine_sessions.py::test_guard_leaves_idle_to_an_unreachable_watchdog).
LOCAL_IDLE = {"idle_timeout": "1m", "watchdog": {"enabled": False}}


# -- idempotency (spec section 23) ------------------------------------------


def test_setup_twice_does_not_create_two_instances(engine):
    first = engine.setup(wait=False)
    second = engine.setup(wait=False)
    assert first.instance_id == second.instance_id
    assert engine.provider.counter == 1


def test_setup_refuses_to_reuse_an_incompatible_instance(write_config):
    config = write_config()
    engine = make_engine(config)
    engine.setup(wait=False)

    changed = write_config(model={"id": "org/some-other-model"})
    other = make_engine(changed)
    other.provider.instances = engine.provider.instances
    with pytest.raises(ConfigError) as exc:
        other.setup(wait=False)
    assert "--recreate" in str(exc.value)


def test_recreate_replaces_the_instance(write_config):
    config = write_config()
    engine = make_engine(config)
    first = engine.setup(wait=False)
    second = engine.setup(wait=False, recreate=True)
    assert second.instance_id != first.instance_id
    assert ("destroy", first.instance_id) in engine.provider.calls


def test_stop_on_an_already_stopped_instance_succeeds(engine):
    engine.setup(wait=False)
    engine.stop()
    result = engine.stop()
    assert result["changed"] is False


def test_destroy_with_nothing_provisioned_succeeds(engine):
    assert engine.destroy()["changed"] is False


# -- reconciliation (spec section 18) ---------------------------------------


def test_status_repairs_state_when_the_instance_vanished(engine):
    state = engine.setup(wait=False)
    engine.provider.instances.clear()  # someone destroyed it in the web console

    payload = engine.status()
    assert payload["state"] == "none"
    assert engine.store.load().instance_id is None
    assert any(
        record["event"] == "state_reconciled" for record in engine.events.tail(limit=50)
    )


def test_reconciliation_marks_orphaned_workloads_failed(engine):
    state = engine.setup(wait=False)
    with engine.store.transaction() as stored:
        from jambu_gpu.core.state import WorkloadRecord

        stored.workloads["w1"] = WorkloadRecord(id="w1", state="running")
    engine.provider.stop(engine.provider.instances[state.instance_id])

    engine.reconcile()
    assert engine.store.load().workloads["w1"].state == "failed"


# -- failure cleanup (spec criterion 4) --------------------------------------


def test_failed_setup_cleans_up_partially_created_resources(engine):
    engine.provider.fail_setup = ProvisioningError("boom")
    with pytest.raises(ProvisioningError):
        engine.setup(wait=False)

    events = [record["event"] for record in engine.events.tail(limit=50)]
    assert "setup_failed" in events
    assert engine.store.load().instance_id is None


def test_cleanup_can_be_disabled(write_config):
    config = write_config(lifecycle={"cleanup_on_setup_failure": False})
    engine = make_engine(config)
    engine.provider.fail_setup = ProvisioningError("boom")
    with pytest.raises(ProvisioningError):
        engine.setup(wait=False)
    assert "cleanup_performed" not in [r["event"] for r in engine.events.tail(limit=50)]


# -- budget (spec section 17) ------------------------------------------------


def test_provisioning_above_the_hourly_budget_fails(write_config):
    config = write_config(budget={"max_hourly_cost_usd": 0.10})
    engine = make_engine(config)
    engine.provider.offer_price = 0.90
    with pytest.raises(BudgetExceededError) as exc:
        engine.setup(wait=False)
    assert "--allow-cost-override" in str(exc.value)
    assert engine.provider.counter == 0


def test_cost_override_provisions_anyway(write_config):
    config = write_config(budget={"max_hourly_cost_usd": 0.10})
    engine = make_engine(config)
    engine.provider.offer_price = 0.90
    state = engine.setup(wait=False, allow_cost_override=True)
    assert state.instance_id is not None


# -- guard (spec sections 10, 14) -------------------------------------------


def test_guard_stops_an_idle_unlocked_instance_and_records_the_reason(write_config):
    config = write_config(lifecycle=LOCAL_IDLE)
    engine = make_engine(config)
    state = engine.setup(wait=False)

    with engine.store.transaction() as stored:
        stored.last_activity = utc_now() - 3600
        stored.starting = False

    decision = engine.guard_tick()
    assert decision["action"] == "stop"
    assert decision["reason"] == "idle_timeout"

    guard_events = [
        r for r in engine.events.tail(limit=50) if r["event"] == "lifecycle_guard_triggered"
    ]
    assert guard_events and guard_events[-1]["reason"] == "idle_timeout"
    assert ("stop", state.instance_id) in engine.provider.calls


def test_guard_respects_an_active_lock(write_config):
    config = write_config(lifecycle=LOCAL_IDLE)
    engine = make_engine(config)
    engine.setup(wait=False)
    with engine.store.transaction() as stored:
        stored.last_activity = utc_now() - 3600
        stored.starting = False
    engine.lock(3600, reason="benchmark running")

    assert engine.guard_tick()["action"] == "none"


def test_expired_lock_stops_protecting(write_config):
    config = write_config(lifecycle=LOCAL_IDLE)
    engine = make_engine(config)
    engine.setup(wait=False)
    with engine.store.transaction() as stored:
        stored.last_activity = utc_now() - 3600
        stored.starting = False
        stored.locked = True
        stored.locked_until = utc_now() - 1

    assert engine.guard_tick()["action"] == "stop"


def test_indefinite_lock_is_refused_by_default(engine):
    engine.setup(wait=False)
    with pytest.raises(ConfigError):
        engine.lock(None)


def test_dry_run_guard_changes_nothing(write_config):
    config = write_config(lifecycle=LOCAL_IDLE)
    engine = make_engine(config)
    engine.setup(wait=False)
    with engine.store.transaction() as stored:
        stored.last_activity = utc_now() - 3600
        stored.starting = False

    assert engine.guard_tick(apply=False)["action"] == "stop"
    assert not [c for c in engine.provider.calls if c[0] == "stop"]


# -- validation --------------------------------------------------------------


def test_validation_flags_an_unbounded_lifecycle(write_config):
    config = write_config(lifecycle={"idle_timeout": None, "max_lifetime": None})
    result = make_engine(config).validate()
    assert not result.ok
    assert any("idle_timeout" in issue.message for issue in result.errors)


def test_validation_warns_when_the_watchdog_is_disabled(write_config):
    config = write_config(lifecycle={"watchdog": {"enabled": False}})
    result = make_engine(config).validate()
    assert any("watchdog" in (issue.field or "") for issue in result.warnings)


# -- workload environment ----------------------------------------------------


def test_workload_env_points_at_the_remote_runtime(engine):
    state = engine.setup(wait=False)
    state.endpoint = "http://127.0.0.1:9"
    env = engine.workload_env(state, "w1")
    assert env["JAMBU_GPU_ENDPOINT"] == "http://127.0.0.1:9"
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:9/v1"
    assert env["JAMBU_MODEL_ID"] == "empero-ai/Qwythos-9B-v2"
    assert env["JAMBU_WORKLOAD_ID"] == "w1"


# -- provider isolation (spec criterion 2, 11) -------------------------------


def test_the_core_never_branches_on_a_provider_name():
    import pathlib

    core = pathlib.Path(__file__).resolve().parents[1] / "jambu_gpu" / "core"
    offenders = []
    for path in core.rglob("*.py"):
        text = path.read_text()
        for needle in ('== "vast"', "== 'vast'", 'name == "vast"'):
            if needle in text:
                offenders.append(str(path))
    assert not offenders, f"core must not branch on provider names: {offenders}"


def test_a_new_provider_needs_only_the_contract(config):
    """The engine drives the fake provider end to end with zero core changes."""
    engine = make_engine(config)
    state = engine.setup(wait=False)
    assert state.provider == "fake"
    assert engine.status()["provider"] == "fake"
    assert engine.logs() == ["line one", "line two"]
    engine.destroy()
    assert engine.store.load().instance_id is None


# -- run flow (spec section 16) ---------------------------------------------


def test_run_executes_the_command_and_records_the_workload(engine, monkeypatch):
    state = engine.setup(wait=False)
    state.endpoint = "http://127.0.0.1:9"
    monkeypatch.setattr(engine, "ensure_ready", lambda: state)

    import sys

    assert engine.run([sys.executable, "-c", "print('hello')"], label="smoke") == 0

    stored = engine.store.load()
    record = list(stored.workloads.values())[-1]
    assert record.state == "finished"
    assert record.returncode == 0
    assert record.label == "smoke"

    events = [r["event"] for r in engine.events.tail(limit=50)]
    assert "workload_started" in events and "workload_finished" in events


def test_a_failing_workload_propagates_its_exit_code(engine, monkeypatch):
    from jambu_gpu.core.errors import WorkloadFailed

    state = engine.setup(wait=False)
    monkeypatch.setattr(engine, "ensure_ready", lambda: state)

    import sys

    with pytest.raises(WorkloadFailed) as exc:
        engine.run([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert exc.value.returncode == 3
    assert list(engine.store.load().workloads.values())[-1].state == "failed"


def test_the_instance_is_not_stopped_after_a_workload(engine, monkeypatch):
    """Sequential experiments reuse the instance; idle_timeout governs teardown."""
    state = engine.setup(wait=False)
    monkeypatch.setattr(engine, "ensure_ready", lambda: state)

    import sys

    engine.run([sys.executable, "-c", "pass"])
    assert engine.store.load().instance_id == state.instance_id
    assert not [call for call in engine.provider.calls if call[0] in ("stop", "destroy")]


def test_run_marks_activity_so_the_guard_sees_it(engine, monkeypatch):
    config_state = engine.setup(wait=False)
    monkeypatch.setattr(engine, "ensure_ready", lambda: config_state)

    import sys

    before = engine.store.load().last_activity
    engine.run([sys.executable, "-c", "pass"])
    assert engine.store.load().last_activity >= before


# -- billable-time bookkeeping (found live: `status` checked long after an
# instance auto-stopped kept reporting cost as if it billed the whole
# wall-clock gap, not just the time it was actually running) ----------------


def test_session_cost_stops_growing_once_the_instance_is_stopped(engine):
    """The instance actually stopped 15 simulated minutes after creation
    (running_since already cleared, billed_seconds closed at that point) -
    checking status hours of wall-clock time later must not add a cent.
    """
    state = engine.setup(wait=False)
    engine.provider.instances[state.instance_id].state = InstanceState.STOPPED
    with engine.store.transaction() as stored:
        stored.state = "stopped"
        stored.running_since = None  # already closed by an earlier reconcile
        stored.billed_seconds = 900.0  # ran for 15 real minutes before stopping
        stored.created_at = utc_now() - 99999  # then hours passed before anyone checked

    payload = engine.status()
    expected = engine.provider.offer_price * 900.0 / 3600.0
    assert payload["session_cost_usd"] == pytest.approx(expected, abs=0.01)


def test_stopping_a_running_instance_freezes_the_cost_at_the_stop_point(write_config):
    config = write_config()
    engine = make_engine(config)
    state = engine.setup(wait=False)
    with engine.store.transaction() as stored:
        stored.created_at = utc_now() - 10
        stored.running_since = stored.created_at  # ran for ~10s

    engine.stop()
    cost_at_stop = engine.store.load().session_cost_usd
    expected = engine.provider.offer_price * 10.0 / 3600.0
    assert cost_at_stop == pytest.approx(expected, abs=0.01)
    assert cost_at_stop > 0

    # Checking status again much later must not inflate it further.
    with engine.store.transaction() as stored:
        stored.created_at = utc_now() - 99999  # simulate a long time passing
    payload = engine.status()
    assert payload["session_cost_usd"] == pytest.approx(cost_at_stop, abs=0.01)


def test_setup_starts_the_billable_clock_immediately_not_at_the_next_reconcile(engine):
    state = engine.setup(wait=False)
    assert engine.store.load().running_since is not None


# -- the runtime image sets the CUDA floor ------------------------------------


def _engine(write_config, **overrides):
    config = write_config(**overrides)
    return Engine(config, provider=FakeProvider(config, CredentialResolver(config.project_dir)))


def test_the_spec_asks_for_the_cuda_the_image_needs(write_config):
    assert _engine(write_config).build_spec().gpu.cuda_min == "12.9"
    latest = _engine(write_config, compute={"image": "vllm/vllm-openai:latest"})
    assert latest.build_spec().gpu.cuda_min == "13.0"


def test_a_stricter_configured_cuda_floor_wins_and_a_looser_one_loses(write_config):
    strict = _engine(write_config, compute={"gpu": {"cuda_min": "13.1"}})
    assert strict.build_spec().gpu.cuda_min == "13.1"
    loose = _engine(write_config, compute={"gpu": {"cuda_min": "12.1"}})
    assert loose.build_spec().gpu.cuda_min == "12.9"


def test_an_image_of_unknown_cuda_keeps_the_configured_floor(write_config):
    custom = {"image": "someone/server:v2"}
    assert _engine(write_config, compute=custom).build_spec().gpu.cuda_min is None
    pinned = dict(custom, gpu={"cuda_min": "12.4"})
    assert _engine(write_config, compute=pinned).build_spec().gpu.cuda_min == "12.4"


def test_validate_warns_about_an_unpinned_image(write_config):
    for image in ("vllm/vllm-openai:latest", "vllm/vllm-openai"):
        result = _engine(write_config, compute={"image": image}).validate(check_provider=False)
        assert any("unpinned image" in issue.message for issue in result.warnings), image
    pinned = _engine(write_config).validate(check_provider=False)
    assert not any("unpinned image" in issue.message for issue in pinned.warnings)


def test_validate_previews_offers_with_the_image_cuda_floor(write_config):
    engine = _engine(write_config)
    seen = {}

    def _validate(config):
        seen["cuda_min"] = config.compute.gpu.cuda_min
        from jambu_gpu.core.models import ValidationResult

        return ValidationResult()

    engine.provider.validate = _validate
    engine.validate()
    assert seen["cuda_min"] == "12.9"
    assert engine.config.compute.gpu.cuda_min is None  # the loaded config is untouched


def test_the_watchdog_learns_the_runtimes_pid_and_log(engine, monkeypatch):
    import json
    import re
    import base64
    import zlib

    script = engine._build_onstart("t" * 43)
    blob = re.search(r"echo (\S+) > /etc/jambu/watchdog.json.b64", script).group(1)
    config = json.loads(zlib.decompress(base64.b64decode(blob)))
    assert config["runtime_pid_path"] == "/var/run/jambu/vllm.pid"
    assert config["logs"] == {
        "runtime": "/var/log/jambu/vllm.log",
        "onstart": "/var/log/jambu/onstart.log",
        "watchdog": "/var/log/jambu/watchdog.log",
    }


# -- a model server that died at boot fails setup fast, with its log ----------


class _DeadRuntimeWatchdog:
    """A reachable watchdog reporting that the model server process is gone."""

    def __init__(self, alive=False):
        self.alive = alive
        self.log_requests = []

    def ping(self):
        return True

    def identify(self, *_args, **_kwargs):
        return {"ok": True}

    def mark_starting(self):
        return {"ok": True}

    def mark_ready(self):
        return {"ok": True}

    def try_state(self):
        return {"starting": False, "runtime_alive": self.alive, "terminated": False}

    def events(self, limit=100):
        return []

    def logs(self, name="runtime", lines=200):
        self.log_requests.append((name, lines))
        return [
            "INFO starting vLLM API server",
            "RuntimeError: The NVIDIA driver on your system is too old "
            "(found version 12020). cuda>=13.0 required.",
        ][-lines:]


def test_a_dead_model_server_aborts_setup_with_its_log_and_cleans_up(engine, monkeypatch):
    import time as _time

    from jambu_gpu.core.errors import RuntimeStartupError

    watchdog = _DeadRuntimeWatchdog()
    monkeypatch.setattr(engine, "beacon", lambda _state: watchdog)
    started = _time.time()
    with pytest.raises(RuntimeStartupError) as caught:
        engine.setup()
    assert _time.time() - started < 10, "must not wait out the 10m startup timeout"
    message = str(caught.value)
    assert "model server process exited" in message
    assert "NVIDIA driver on your system is too old" in message
    assert watchdog.log_requests == [("runtime", 30)]
    # cleanup_on_setup_failure: the dead instance is not left billing
    assert any(call[0] == "destroy" for call in engine.provider.calls)
    assert engine.store.load().instance_id is None


def test_an_unknown_runtime_liveness_does_not_abort(engine, monkeypatch):
    """None (no pid file yet) and True keep waiting on the health probe."""
    for alive in (None, True):
        watchdog = _DeadRuntimeWatchdog(alive=alive)
        monkeypatch.setattr(engine, "beacon", lambda _state, w=watchdog: w)
        state = engine.setup(wait=False)
        assert engine._runtime_exited(state) is None
        assert watchdog.log_requests == []


# -- gpu logs: the instance's own files through the watchdog -----------------


def test_logs_default_to_the_runtime_log_when_the_watchdog_answers(engine, monkeypatch):
    engine.setup(wait=False)
    watchdog = _DeadRuntimeWatchdog()
    monkeypatch.setattr(engine, "beacon", lambda _state: watchdog)
    source, lines = engine.read_logs(limit=1)
    assert source == "runtime"
    assert lines == [watchdog.logs()[-1]]
    assert engine.read_logs(limit=5, source="onstart")[0] == "onstart"
    assert watchdog.log_requests[-1] == ("onstart", 5)
    assert engine.read_logs(source="provider") == ("provider", ["line one", "line two"])


def test_logs_fall_back_to_the_provider_when_the_watchdog_is_unreachable(engine):
    from jambu_gpu.core.errors import StateError

    engine.setup(wait=False)  # the fake's watchdog endpoint refuses connections
    assert engine.read_logs() == ("provider", ["line one", "line two"])
    with pytest.raises(StateError, match="cannot read the watchdog log"):
        engine.read_logs(source="watchdog")
    with pytest.raises(ConfigError, match="unknown log source"):
        engine.read_logs(source="kernel")

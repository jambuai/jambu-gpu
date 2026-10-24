"""Account-wide control: list_instances, `gpu ls`, `gpu sweep`, the systemd timer."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import respx
from fakes import FakeProvider
from typer.testing import CliRunner

from jambu_gpu.cli import account as account_cli
from jambu_gpu.cli.main import app
from jambu_gpu.core import sweep as sweep_mod
from jambu_gpu.core.credentials import CredentialResolver
from jambu_gpu.core.engine import Engine
from jambu_gpu.core.errors import ProviderError, UnsupportedCapabilityError
from jambu_gpu.core.events import EventLog
from jambu_gpu.core.models import Instance, InstanceState, ProviderCapabilities
from jambu_gpu.core.state import RuntimeState, utc_now
from jambu_gpu.providers.base import GPUProvider
from jambu_gpu.providers.vast.config import API_BASE
from jambu_gpu.providers.vast.provider import VastProvider

runner = CliRunner()
HOUR = 3600.0


@pytest.fixture(autouse=True)
def jambu_home(tmp_path, monkeypatch):
    home = tmp_path / "jambu-home"
    monkeypatch.setenv("JAMBU_HOME", str(home))
    return home


@pytest.fixture
def fake(config):
    return FakeProvider(config, CredentialResolver(config.project_dir))


def add(provider, instance_id, state, age_s, label="jambu-model-abc123"):
    provider.instances[instance_id] = Instance(
        id=instance_id,
        provider=provider.name,
        state=state,
        created_at=utc_now() - age_s,
        gpu_name="RTX 4090",
        gpu_count=1,
        hourly_cost_usd=0.4,
        label=label,
    )
    return provider.instances[instance_id]


def run_sweep(provider, apply=True, tracked=None, registry=None, now=None, events=None, **kw):
    return sweep_mod.sweep(
        provider,
        max_lifetime_s=kw.get("max_lifetime_s", 6 * HOUR),
        stopped_ttl_s=kw.get("stopped_ttl_s", 24 * HOUR),
        apply=apply,
        tracked=tracked,
        registry=registry or sweep_mod.SweepRegistry(),
        events=events,
        now=now,
    )


# -- provider contract ---------------------------------------------------------


def test_list_instances_is_an_optional_capability(config):
    assert ProviderCapabilities().list_instances is False

    class Minimal(FakeProvider):
        capabilities = ProviderCapabilities(stop=True)

        def list_instances(self):
            return GPUProvider.list_instances(self)

    with pytest.raises(UnsupportedCapabilityError):
        Minimal(config, CredentialResolver()).list_instances()
    with pytest.raises(UnsupportedCapabilityError):
        sweep_mod.list_jambu_instances(Minimal(config, CredentialResolver()))


@respx.mock
def test_vast_lists_only_jambu_labelled_instances(config, monkeypatch):
    monkeypatch.setenv("VAST_API_KEY", "test-key")
    provider = VastProvider(config, CredentialResolver(config.project_dir))
    assert provider.capabilities.list_instances is True
    route = respx.get(f"{API_BASE}/instances/").mock(
        return_value=httpx.Response(
            200,
            json={
                "instances": [
                    {"id": 1, "label": "jambu-Qwythos-9B-v2-abc123", "actual_status": "running",
                     "dph_total": 0.35, "gpu_name": "RTX 4090", "num_gpus": 1,
                     "start_date": 1_700_000_000.0},
                    {"id": 2, "label": "my-own-training-run", "actual_status": "running"},
                    {"id": 3, "label": None, "actual_status": "stopped"},
                    {"id": 4, "label": "jambu-other-def456", "actual_status": "exited"},
                ]
            },
        )
    )
    found = provider.list_instances()
    assert route.calls[0].request.url.params["owner"] == "me"
    assert [i.id for i in found] == ["1", "4"]
    assert found[0].state is InstanceState.READY
    assert found[0].hourly_cost_usd == 0.35
    assert found[1].state is InstanceState.STOPPED


def test_the_engine_labels_instances_so_the_sweep_can_find_them(config, fake):
    engine = Engine(config, provider=fake)
    state = engine.setup(wait=False)
    assert [i.id for i in sweep_mod.list_jambu_instances(fake)] == [state.instance_id]


# -- sweep rules ----------------------------------------------------------------


def test_a_running_orphan_past_max_lifetime_and_grace_is_destroyed(fake):
    add(fake, "old", InstanceState.READY, 7 * HOUR)
    add(fake, "young", InstanceState.READY, 6 * HOUR + 600)  # inside the 30m grace

    result = run_sweep(fake)
    assert [(a.instance.id, a.action, a.reason) for a in result.actions] == [
        ("old", "destroy", "max_lifetime_exceeded")
    ]
    assert ("destroy", "old") in fake.calls
    assert "young" in fake.instances


def test_the_tracked_instance_is_stopped_not_destroyed(fake):
    add(fake, "mine", InstanceState.IDLE, 8 * HOUR)
    tracked = RuntimeState(instance_id="mine", provider="fake")

    result = run_sweep(fake, tracked=tracked)
    assert result.actions[0].action == "stop" and result.actions[0].tracked
    assert ("stop", "mine") in fake.calls
    assert fake.instances["mine"].state is InstanceState.STOPPED


def test_the_tracked_session_start_wins_over_the_provider_creation_time(fake):
    add(fake, "mine", InstanceState.READY, 8 * HOUR)
    tracked = RuntimeState(instance_id="mine", provider="fake", session_started_at=utc_now() - 600)
    assert run_sweep(fake, tracked=tracked).actions == []


def test_a_locally_locked_tracked_instance_is_left_alone(fake):
    add(fake, "mine", InstanceState.READY, 8 * HOUR)
    tracked = RuntimeState(
        instance_id="mine", provider="fake", locked=True, locked_until=utc_now() + HOUR
    )
    result = run_sweep(fake, tracked=tracked)
    assert result.actions[0].action == "none"
    assert not [c for c in fake.calls if c[0] in ("stop", "destroy")]


def test_non_jambu_instances_are_never_touched(fake):
    add(fake, "theirs", InstanceState.READY, 100 * HOUR, label="someone-elses-job")
    add(fake, "unlabelled", InstanceState.STOPPED, 100 * HOUR, label="")

    class Sloppy(FakeProvider):
        def list_instances(self):  # an adapter that forgot to filter
            return list(self.instances.values())

    sloppy = Sloppy(fake.config, CredentialResolver())
    sloppy.instances = fake.instances
    result = run_sweep(sloppy, now=utc_now() + 100 * HOUR)
    assert result.seen == 0 and result.actions == []
    assert not [c for c in sloppy.calls if c[0] in ("stop", "destroy")]


def test_a_stopped_instance_is_destroyed_once_seen_stopped_past_stopped_ttl(fake, jambu_home):
    add(fake, "parked", InstanceState.STOPPED, 2 * HOUR)
    t0 = utc_now()

    first = run_sweep(fake, now=t0)
    assert first.actions == []
    saved = json.loads((jambu_home / "sweep.json").read_text())
    assert "fake:parked" in saved["stopped"]

    # The first sighting is kept across runs; a day later it goes.
    assert run_sweep(fake, now=t0 + 12 * HOUR).actions == []
    later = run_sweep(fake, now=t0 + 25 * HOUR)
    assert [(a.action, a.reason) for a in later.actions] == [("destroy", "stopped_ttl")]
    assert "parked" not in fake.instances
    assert "fake:parked" not in json.loads((jambu_home / "sweep.json").read_text())["stopped"]


def test_the_tracked_stopped_since_counts_as_a_first_sighting(fake):
    add(fake, "mine", InstanceState.STOPPED, 30 * HOUR)
    tracked = RuntimeState(instance_id="mine", provider="fake", stopped_since=utc_now() - 25 * HOUR)
    result = run_sweep(fake, tracked=tracked)
    assert [(a.action, a.reason) for a in result.actions] == [("destroy", "stopped_ttl")]


def test_registry_entries_go_when_the_instance_runs_again_or_disappears(fake, jambu_home):
    add(fake, "a", InstanceState.STOPPED, HOUR)
    add(fake, "b", InstanceState.STOPPED, HOUR)
    run_sweep(fake)
    assert set(json.loads((jambu_home / "sweep.json").read_text())["stopped"]) == {
        "fake:a",
        "fake:b",
    }

    fake.instances["a"].state = InstanceState.READY
    del fake.instances["b"]
    run_sweep(fake)
    assert json.loads((jambu_home / "sweep.json").read_text())["stopped"] == {}


def test_a_dry_run_changes_nothing_but_still_records_sightings(fake, jambu_home):
    add(fake, "old", InstanceState.READY, 9 * HOUR)
    add(fake, "parked", InstanceState.STOPPED, HOUR)
    result = run_sweep(fake, apply=False)
    assert result.actions[0].action == "destroy" and not result.actions[0].applied
    assert not [c for c in fake.calls if c[0] in ("stop", "destroy")]
    assert "fake:parked" in json.loads((jambu_home / "sweep.json").read_text())["stopped"]


def test_null_thresholds_disable_the_rules(fake):
    add(fake, "old", InstanceState.READY, 99 * HOUR)
    add(fake, "parked", InstanceState.STOPPED, HOUR)
    result = run_sweep(fake, max_lifetime_s=None, stopped_ttl_s=None, now=utc_now() + 99 * HOUR)
    assert result.actions == []


def test_every_action_emits_an_event_with_its_reason(fake, tmp_path):
    add(fake, "old", InstanceState.READY, 9 * HOUR)
    log = EventLog(tmp_path / "logs")
    run_sweep(fake, events=[log])
    records = log.tail()
    sweep_events = [r for r in records if r["event"] == "sweep_action"]
    assert sweep_events[0]["reason"] == "max_lifetime_exceeded"
    assert sweep_events[0]["applied"] is True
    destroyed = [r for r in records if r["event"] == "instance_destroyed"]
    assert destroyed[0]["reason"] == "max_lifetime_exceeded"
    assert destroyed[0]["source"] == "sweep"


def test_a_provider_error_on_one_instance_does_not_stop_the_sweep(fake):
    add(fake, "a", InstanceState.READY, 9 * HOUR)
    add(fake, "b", InstanceState.READY, 9 * HOUR)
    real_destroy = fake.destroy

    def flaky(instance):
        if instance.id == "a":
            raise ProviderError("api blip")
        real_destroy(instance)

    fake.destroy = flaky
    result = run_sweep(fake)
    by_id = {a.instance.id: a for a in result.actions}
    assert by_id["a"].error == "api blip" and not by_id["a"].applied
    assert by_id["b"].applied and "b" not in fake.instances


def test_a_corrupt_registry_does_not_block_the_sweep(fake, jambu_home):
    jambu_home.mkdir(parents=True)
    (jambu_home / "sweep.json").write_text("{not json")
    add(fake, "old", InstanceState.READY, 9 * HOUR)
    assert run_sweep(fake).actions[0].applied


# -- CLI ------------------------------------------------------------------------


@pytest.fixture
def cli_fake(monkeypatch, tmp_path):
    """Every CLI invocation in a test talks to the same in-memory account."""
    holder = {}

    def _make(config, credentials, name):
        if "provider" not in holder:
            holder["provider"] = FakeProvider(config, credentials)
        return holder["provider"]

    monkeypatch.setattr(account_cli, "make_provider", _make)
    monkeypatch.delenv("VAST_API_KEY", raising=False)
    return holder


def _seed(cli_fake, config):
    provider = FakeProvider(config, CredentialResolver())
    cli_fake["provider"] = provider
    return provider


def test_ls_lists_account_instances_and_marks_the_tracked_one(config, cli_fake, monkeypatch):
    monkeypatch.chdir(config.project_dir)
    provider = _seed(cli_fake, config)
    engine = Engine(config, provider=provider)
    mine = engine.setup(wait=False)
    add(provider, "elsewhere", InstanceState.STOPPED, 3 * HOUR, label="jambu-other-000000")
    add(provider, "theirs", InstanceState.READY, HOUR, label="not-ours")

    result = runner.invoke(app, ["--json", "ls"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    rows = {r["instance_id"]: r for r in payload["instances"]}
    assert set(rows) == {mine.instance_id, "elsewhere"}
    assert rows[mine.instance_id]["tracked"] is True
    assert rows["elsewhere"]["tracked"] is False
    assert rows["elsewhere"]["state"] == "stopped"
    assert rows["elsewhere"]["hourly_cost_usd"] == 0.4
    assert rows["elsewhere"]["age_seconds"] == pytest.approx(3 * HOUR, abs=5)

    table = runner.invoke(app, ["ls"])
    assert table.exit_code == 0
    assert "elsewhere" in table.stdout and "theirs" not in table.stdout


def test_ls_works_without_a_project(tmp_path, cli_fake, monkeypatch, config):
    empty = tmp_path / "nowhere"
    empty.mkdir()
    monkeypatch.chdir(empty)
    provider = _seed(cli_fake, config)
    add(provider, "x", InstanceState.READY, HOUR)
    result = runner.invoke(app, ["--json", "ls"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["instances"][0]["tracked"] is False


def test_sweep_is_a_dry_run_by_default(config, cli_fake, monkeypatch):
    monkeypatch.chdir(config.project_dir)
    provider = _seed(cli_fake, config)
    add(provider, "old", InstanceState.READY, 9 * HOUR)

    result = runner.invoke(app, ["sweep"])
    assert result.exit_code == 0, result.output
    assert "would destroy old" in result.stdout
    assert "--apply" in result.stdout
    assert "old" in provider.instances

    applied = runner.invoke(app, ["--json", "sweep", "--apply"])
    assert applied.exit_code == 0, applied.output
    payload = json.loads(applied.stdout)
    assert payload["applied"] is True
    assert payload["actions"][0]["reason"] == "max_lifetime_exceeded"
    assert "old" not in provider.instances


def test_sweep_uses_project_thresholds_and_flags_override_them(write_config, cli_fake, monkeypatch):
    config = write_config(lifecycle={"max_lifetime": "12h", "idle_timeout": "15m"})
    monkeypatch.chdir(config.project_dir)
    provider = _seed(cli_fake, config)
    add(provider, "nine-hours", InstanceState.READY, 9 * HOUR)

    payload = json.loads(runner.invoke(app, ["--json", "sweep"]).stdout)
    assert payload["max_lifetime_s"] == 12 * HOUR and payload["actions"] == []

    payload = json.loads(runner.invoke(app, ["--json", "sweep", "--max-lifetime", "6h"]).stdout)
    assert payload["actions"][0]["instance_id"] == "nine-hours"


def test_sweep_without_a_project_uses_defaults(tmp_path, cli_fake, monkeypatch, config, jambu_home):
    empty = tmp_path / "nowhere"
    empty.mkdir()
    monkeypatch.chdir(empty)
    provider = _seed(cli_fake, config)
    add(provider, "old", InstanceState.READY, 7 * HOUR)

    result = runner.invoke(app, ["--json", "sweep", "--apply"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["max_lifetime_s"] == 6 * HOUR
    assert payload["stopped_ttl_s"] == 24 * HOUR
    assert payload["actions"][0]["applied"] is True
    # Events land in $JAMBU_HOME when there is no project to log into.
    events = (jambu_home / "logs" / "events.jsonl").read_text()
    assert "sweep_action" in events and "max_lifetime_exceeded" in events


def test_sweep_stops_the_project_instance_and_logs_into_the_project(config, cli_fake, monkeypatch):
    monkeypatch.chdir(config.project_dir)
    provider = _seed(cli_fake, config)
    engine = Engine(config, provider=provider)
    state = engine.setup(wait=False)
    with engine.store.transaction() as stored:
        stored.session_started_at = utc_now() - 8 * HOUR

    payload = json.loads(runner.invoke(app, ["--json", "sweep", "--apply"]).stdout)
    assert payload["actions"][0]["action"] == "stop"
    assert payload["actions"][0]["tracked"] is True
    assert ("stop", state.instance_id) in provider.calls
    assert any(r["event"] == "sweep_action" for r in engine.events.tail())


def test_sweep_rejects_a_bad_duration_cleanly(config, cli_fake, monkeypatch):
    monkeypatch.chdir(config.project_dir)
    _seed(cli_fake, config)
    result = runner.invoke(app, ["sweep", "--stopped-ttl", "soon"])
    assert result.exit_code == 2
    assert "--stopped-ttl" in result.output


# -- systemd timer -------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


def test_install_timer_writes_user_units_for_sweep_and_project_guard(config, home, monkeypatch):
    monkeypatch.chdir(config.project_dir)
    result = runner.invoke(app, ["sweep", "--install-timer", "--every", "10m"])
    assert result.exit_code == 0, result.output

    unit_dir = home / ".config" / "systemd" / "user"
    service = (unit_dir / "jambu-gpu-sweep.service").read_text()
    timer = (unit_dir / "jambu-gpu-sweep.timer").read_text()
    assert f"ExecStart=-{sys.executable} -m jambu_gpu sweep --apply" in service
    assert f"-m jambu_gpu --config {config.source_path} guard" in service
    assert f"WorkingDirectory={config.project_dir}" in service
    assert "Type=oneshot" in service
    assert "OnUnitActiveSec=600s" in timer
    assert "WantedBy=timers.target" in timer
    printed = " ".join(result.stdout.split())  # rich wraps long lines
    assert "systemctl --user enable --now jambu-gpu-sweep.timer" in printed


def test_install_timer_keeps_other_projects_guards_and_does_not_duplicate(
    write_config, home, monkeypatch, tmp_path
):
    config = write_config()
    monkeypatch.chdir(config.project_dir)
    runner.invoke(app, ["sweep", "--install-timer"])
    runner.invoke(app, ["sweep", "--install-timer"])

    other = tmp_path / "other project"
    other.mkdir()
    (other / "jambu.yaml").write_text(config.source_path.read_text())
    monkeypatch.chdir(other)
    assert runner.invoke(app, ["sweep", "--install-timer"]).exit_code == 0

    service = (home / ".config/systemd/user/jambu-gpu-sweep.service").read_text()
    guards = [line for line in service.splitlines() if line.endswith(" guard")]
    assert len(guards) == 2
    assert f'"{other / "jambu.yaml"}"' in service  # quoted: the path has a space


def test_install_timer_without_a_project_only_sweeps(tmp_path, home, monkeypatch):
    empty = tmp_path / "nowhere"
    empty.mkdir()
    monkeypatch.chdir(empty)
    result = runner.invoke(app, ["--json", "sweep", "--install-timer"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["guards"] == 0 and payload["project_config"] is None
    service = Path(payload["service"]).read_text()
    assert not [line for line in service.splitlines() if line.endswith(" guard")]
    assert "sweep --apply" in service


def test_uninstall_timer_removes_the_units(config, home, monkeypatch):
    monkeypatch.chdir(config.project_dir)
    runner.invoke(app, ["sweep", "--install-timer"])
    result = runner.invoke(app, ["--json", "sweep", "--uninstall-timer"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert len(payload["removed"]) == 2
    assert "disable --now" in payload["disable"]
    assert not (home / ".config/systemd/user/jambu-gpu-sweep.timer").exists()
    assert runner.invoke(app, ["sweep", "--uninstall-timer"]).exit_code == 0


def test_the_module_entrypoint_the_timer_runs_works():
    repo = Path(__file__).resolve().parents[1]
    out = subprocess.run(
        [sys.executable, "-m", "jambu_gpu", "version"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "0.1.0"

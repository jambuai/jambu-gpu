"""The remote watchdog, exercised for real: the generated script is executed
as its own process and driven over HTTP, exactly as it runs on the instance.

This is the guarantee behind spec criteria 6, 8, 9 and 12: lifecycle
enforcement does not depend on the local CLI staying alive.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jambu_gpu.core.beacon import WatchdogClient
from jambu_gpu.core.policy import PolicyConfig
from jambu_gpu.watchdog.builder import (
    build_agent_config,
    build_agent_source,
    build_onstart_script,
    generate_token,
)

# Provider actions that record what they were asked to do instead of calling an API.
MARKER_ACTIONS = '''
import json as _json
import os as _os


def _record(action, ctx):
    path = _os.environ["JAMBU_TEST_MARKER"]
    with open(path, "w") as handle:
        _json.dump({"action": action, "instance_id": ctx.get("instance_id")}, handle)


def provider_stop(ctx):
    _record("stop", ctx)


def provider_destroy(ctx):
    _record("destroy", ctx)
'''


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Agent:
    def __init__(self, tmp_path: Path, policy: PolicyConfig, **overrides):
        self.dir = tmp_path
        self.token = generate_token()
        self.port = free_port()
        self.marker = tmp_path / "marker.json"
        self.events = tmp_path / "events.jsonl"

        config = build_agent_config(
            provider="test",
            instance_id="42",
            token=self.token,
            port=self.port,
            interval_s=overrides.pop("interval_s", 1.0),
            startup_grace_s=overrides.pop("startup_grace_s", 1.0),
            policy=policy,
            created_at=overrides.pop("created_at", time.time()),
        )
        config.update(overrides)
        (tmp_path / "watchdog.json").write_text(json.dumps(config))
        (tmp_path / "agent.py").write_text(build_agent_source(MARKER_ACTIONS))

        env = dict(os.environ)
        env.update(
            {
                "JAMBU_WATCHDOG_CONFIG": str(tmp_path / "watchdog.json"),
                "JAMBU_WATCHDOG_STATE": str(tmp_path / "activity.json"),
                "JAMBU_WATCHDOG_EVENTS": str(self.events),
                "JAMBU_TEST_MARKER": str(self.marker),
            }
        )
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(tmp_path / "agent.py")],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.client = WatchdogClient(f"http://127.0.0.1:{self.port}", self.token, timeout=5.0)
        self._await_boot()

    def _await_boot(self) -> None:
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f"watchdog died: {self.process.stdout.read()}")
            if self.client.ping():
                return
            time.sleep(0.2)
        raise AssertionError("watchdog never came up")

    def wait_for_marker(self, timeout: float = 20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.marker.exists():
                return json.loads(self.marker.read_text())
            time.sleep(0.2)
        return None

    def event_names(self) -> list[str]:
        if not self.events.exists():
            return []
        return [
            json.loads(line)["event"]
            for line in self.events.read_text().splitlines()
            if line.strip()
        ]

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            self.process.kill()


@pytest.fixture
def agent_factory(tmp_path):
    agents: list[Agent] = []

    def _make(policy: PolicyConfig, **overrides) -> Agent:
        directory = tmp_path / f"agent{len(agents)}"
        directory.mkdir()
        agent = Agent(directory, policy, **overrides)
        agents.append(agent)
        return agent

    yield _make
    for agent in agents:
        agent.close()


def policy(**overrides) -> PolicyConfig:
    base = dict(idle_timeout_s=2.0, max_lifetime_s=None, heartbeat_grace_s=2.0)
    base.update(overrides)
    return PolicyConfig(**base)


# ---------------------------------------------------------------------------


def test_generated_agent_is_valid_python():
    source = build_agent_source(MARKER_ACTIONS)
    compile(source, "watchdog.py", "exec")
    assert "def evaluate(" in source  # the core policy really is inlined
    assert "def provider_stop(" in source


def test_the_control_port_requires_the_token(agent_factory):
    agent = agent_factory(policy(idle_timeout_s=3600.0))
    rogue = WatchdogClient(agent.client.base_url, "wrong-token", timeout=5.0)
    assert rogue.ping() is True          # /health is open, for liveness probes
    assert rogue.try_state() is None     # everything else is not


def test_idle_instance_is_stopped_with_a_recorded_reason(agent_factory):
    agent = agent_factory(policy())
    agent.client.mark_ready()

    result = agent.wait_for_marker()
    assert result == {"action": "stop", "instance_id": "42"}

    events = agent.event_names()
    assert "lifecycle_guard_triggered" in events
    assert "instance_stopped" in events

    records = [
        json.loads(line)
        for line in agent.events.read_text().splitlines()
        if line.strip()
    ]
    guard = [r for r in records if r["event"] == "lifecycle_guard_triggered"][-1]
    assert guard["reason"] == "idle_timeout"
    assert guard["action"] == "stop"


def test_a_heartbeating_workload_is_never_stopped(agent_factory):
    agent = agent_factory(policy())
    agent.client.mark_ready()
    agent.client.workload_start("w1", "long eval")

    # Heartbeat across several idle windows.
    for _ in range(12):
        agent.client.workload_heartbeat("w1")
        time.sleep(0.5)

    assert not agent.marker.exists()
    assert agent.client.state()["state"] == "busy"

    # Finish it and the instance goes idle, then stops.
    agent.client.workload_end("w1", 0)
    assert agent.wait_for_marker()["action"] == "stop"


def test_a_vanished_heartbeat_falls_back_to_idle(agent_factory):
    """Criterion 9: the local CLI crashed mid-workload."""
    agent = agent_factory(policy(heartbeat_grace_s=2.0))
    agent.client.mark_ready()
    agent.client.workload_start("w1", "crashed run")
    # No further heartbeats, no workload_end: exactly what a crash looks like.
    assert agent.wait_for_marker(timeout=25)["action"] == "stop"


def test_a_lock_prevents_shutdown_until_it_expires(agent_factory):
    agent = agent_factory(policy())
    agent.client.mark_ready()
    agent.client.lock(ttl_seconds=6, reason="benchmark")

    time.sleep(4)
    assert not agent.marker.exists(), "locked instance must not be stopped"
    assert agent.client.state()["locked"] is True

    assert agent.wait_for_marker(timeout=25)["action"] == "stop"
    assert "instance_unlocked" in agent.event_names()


def test_indefinite_locks_are_refused_by_default(agent_factory):
    agent = agent_factory(policy(idle_timeout_s=3600.0))
    assert agent.client.try_request("POST", "/lock", {"ttl_seconds": None}) is None


def test_indefinite_locks_work_when_explicitly_allowed(agent_factory):
    agent = agent_factory(policy(idle_timeout_s=1.0, allow_indefinite_lock=True))
    agent.client.mark_ready()
    agent.client.lock(ttl_seconds=None, reason="pinned")
    time.sleep(4)
    assert not agent.marker.exists()


def test_max_lifetime_stops_a_busy_instance(agent_factory):
    agent = agent_factory(
        policy(idle_timeout_s=3600.0, max_lifetime_s=3.0),
        created_at=time.time(),
    )
    agent.client.mark_ready()
    agent.client.workload_start("w1", "endless")

    deadline = time.time() + 20
    while time.time() < deadline and not agent.marker.exists():
        agent.client.workload_heartbeat("w1")
        time.sleep(0.5)

    result = agent.wait_for_marker(timeout=5)
    assert result is not None and result["action"] == "stop"


def test_auto_destroy_escalates(agent_factory):
    agent = agent_factory(policy(auto_destroy=True))
    agent.client.mark_ready()
    assert agent.wait_for_marker()["action"] == "destroy"


def test_startup_grace_expires_when_the_cli_never_confirms(agent_factory):
    """The CLI died during setup: the instance must not stay 'starting' forever."""
    agent = agent_factory(policy(), startup_grace_s=2.0)
    # Never call /ready.
    assert agent.wait_for_marker(timeout=25)["action"] == "stop"
    assert "runtime_startup_grace_expired" in agent.event_names()


def test_identify_teaches_the_watchdog_who_it_is(agent_factory):
    agent = agent_factory(policy(idle_timeout_s=3600.0))
    agent.client.identify("99", hourly_cost_usd=1.25)
    state = agent.client.state()
    assert state["instance_id"] == "99"
    assert state["policy"]["hourly_cost_usd"] == 1.25


def test_manual_shutdown_route(agent_factory):
    agent = agent_factory(policy(idle_timeout_s=3600.0))
    agent.client.shutdown("destroy", reason="manual")
    assert agent.wait_for_marker()["action"] == "destroy"


def test_onstart_script_starts_the_watchdog_before_the_model(agent_factory):
    script = build_onstart_script(
        agent_source="print('agent')",
        agent_config={"port": 8777},
        runtime_start_command="nohup vllm serve &",
        runtime_env={"HF_HOME": "/workspace/hf"},
    )
    assert script.startswith("#!/bin/bash")
    assert script.index("watchdog.py") < script.index("vllm serve")
    assert "chmod 600 /etc/jambu/watchdog.json" in script
    assert "export HF_HOME='/workspace/hf'" in script


def test_the_boot_script_reconstitutes_the_agent_byte_for_byte(tmp_path):
    """The agent travels compressed inside a provider API field; prove it survives."""
    source = build_agent_source(MARKER_ACTIONS)
    script = build_onstart_script(
        agent_source=source,
        agent_config={"token": "t", "port": 8777},
        runtime_start_command="echo model",
    )
    assert len(script) < len(source), "the payload must be compressed, not inlined raw"

    root = tmp_path / "root"
    rewritten = script
    for remote, local in (
        ("/etc/jambu", root / "etc"),
        ("/var/lib/jambu", root / "lib"),
        ("/var/log/jambu", root / "log"),
        ("/var/run/jambu", root / "run"),
        ("/opt/jambu", root / "opt"),
    ):
        rewritten = rewritten.replace(remote, str(local))
    boot = tmp_path / "onstart.sh"
    boot.write_text(rewritten)

    subprocess.run(["bash", str(boot)], check=True, timeout=60)
    assert (root / "opt" / "watchdog.py").read_text() == source

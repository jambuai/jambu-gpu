"""Account-wide safety net: `gpu ls` and `gpu sweep`.

The watchdog guards one instance from the inside and `gpu guard` guards the
one instance a project tracks. Neither sees an instance whose watchdog died
and whose project directory is gone. The sweep lists every instance this tool
created in the account and applies two blunt rules to it:

- a running instance older than ``max_lifetime + SWEEP_GRACE_S`` means the
  watchdog failed: stop it if a local project tracks it, destroy it otherwise;
- an instance first seen stopped more than ``stopped_ttl`` ago is destroyed.

Providers do not report when an instance stopped, so the first time the sweep
saw each one stopped is kept in ``$JAMBU_HOME/sweep.json``.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import events as ev
from . import policy
from .errors import ProviderError
from .events import EventLog
from .models import Instance, InstanceState, is_jambu_label
from .state import RuntimeState, from_iso, to_iso, utc_now

# The watchdog acts within one interval of max_lifetime. Half an hour past it
# a running instance has clearly escaped its guard.
SWEEP_GRACE_S = 1800.0
DEFAULT_MAX_LIFETIME_S = 6 * 3600.0
DEFAULT_STOPPED_TTL_S = 24 * 3600.0

REASON_ORPHAN_LIFETIME = "max_lifetime_exceeded"
REASON_LOCKED = "locked"

SWEEP_ACTION = "sweep_action"


def jambu_home() -> Path:
    override = os.environ.get("JAMBU_HOME")
    return Path(override).expanduser() if override else Path.home() / ".jambu"


class SweepRegistry:
    """First-seen-stopped timestamps, keyed by ``provider:instance_id``."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else jambu_home() / "sweep.json"
        self.stopped: dict[str, float] = {}
        self._load()

    @staticmethod
    def key(provider: str, instance_id: str) -> str:
        return f"{provider}:{instance_id}"

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text() or "{}")
        except (OSError, json.JSONDecodeError):
            # A corrupt registry only delays stopped_ttl; never block the sweep.
            return
        for key, value in (data.get("stopped") or {}).items():
            try:
                ts = from_iso(value)
            except Exception:  # noqa: BLE001 - one bad entry must not lose the rest
                continue
            if ts is not None:
                self.stopped[key] = ts

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "stopped": {k: to_iso(v) for k, v in sorted(self.stopped.items())},
        }
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".sweep-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps(payload, indent=2) + "\n")
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def first_seen_stopped(self, key: str, now: float) -> float:
        return self.stopped.setdefault(key, now)

    def forget(self, key: str) -> None:
        self.stopped.pop(key, None)

    def retain(self, provider: str, keys: set[str]) -> None:
        """Drop entries of ``provider`` that are no longer in the account."""
        prefix = f"{provider}:"
        for key in [k for k in self.stopped if k.startswith(prefix) and k not in keys]:
            del self.stopped[key]


@dataclass
class SweepAction:
    instance: Instance
    action: str  # stop | destroy | none
    reason: str
    tracked: bool = False
    age_seconds: Optional[float] = None
    stopped_seconds: Optional[float] = None
    applied: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance.id,
            "label": self.instance.label,
            "state": self.instance.state.value,
            "action": self.action,
            "reason": self.reason,
            "tracked": self.tracked,
            "age_seconds": None if self.age_seconds is None else round(self.age_seconds, 1),
            "stopped_seconds": (
                None if self.stopped_seconds is None else round(self.stopped_seconds, 1)
            ),
            "applied": self.applied,
            "error": self.error,
        }


@dataclass
class SweepResult:
    provider: str
    apply: bool
    max_lifetime_s: Optional[float]
    stopped_ttl_s: Optional[float]
    seen: int = 0
    actions: list[SweepAction] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "applied": self.apply,
            "max_lifetime_s": self.max_lifetime_s,
            "stopped_ttl_s": self.stopped_ttl_s,
            "grace_s": SWEEP_GRACE_S,
            "instances_seen": self.seen,
            "actions": [a.to_dict() for a in self.actions],
        }


def list_jambu_instances(provider) -> list[Instance]:
    provider.require_capability("list_instances")
    # The adapter already filters by label; filter again so a sloppy adapter
    # can never put someone else's machine in front of the sweep.
    return [i for i in provider.list_instances() if is_jambu_label(i.label)]


def _tracked_lock_active(state: RuntimeState, allow_indefinite: bool, now: float) -> bool:
    if not state.locked:
        return False
    if state.locked_until is None:
        return allow_indefinite
    return state.locked_until > now


def sweep(
    provider,
    *,
    max_lifetime_s: Optional[float],
    stopped_ttl_s: Optional[float],
    apply: bool = False,
    registry: Optional[SweepRegistry] = None,
    tracked: Optional[RuntimeState] = None,
    allow_indefinite_lock: bool = False,
    events: Optional[list[EventLog]] = None,
    echo=None,
    now: Optional[float] = None,
) -> SweepResult:
    """Plan (and with ``apply`` perform) the account-wide cleanup."""
    now = now if now is not None else utc_now()
    registry = registry or SweepRegistry()
    sinks = list(events or [])
    say = echo or (lambda _m: None)
    tracked_id = tracked.instance_id if tracked is not None else None

    instances = list_jambu_instances(provider)
    result = SweepResult(
        provider=provider.name,
        apply=apply,
        max_lifetime_s=max_lifetime_s,
        stopped_ttl_s=stopped_ttl_s,
        seen=len(instances),
    )
    present: set[str] = set()

    for instance in instances:
        key = registry.key(provider.name, instance.id)
        present.add(key)
        is_tracked = tracked_id is not None and instance.id == tracked_id

        if instance.state is InstanceState.STOPPED:
            first = registry.first_seen_stopped(key, now)
            if is_tracked and tracked.stopped_since is not None:
                first = min(first, tracked.stopped_since)
                registry.stopped[key] = first
            stopped_for = now - first
            if stopped_ttl_s is not None and stopped_for > stopped_ttl_s:
                result.actions.append(
                    SweepAction(
                        instance,
                        policy.ACTION_DESTROY,
                        policy.REASON_STOPPED_TTL,
                        tracked=is_tracked,
                        stopped_seconds=stopped_for,
                    )
                )
            continue

        registry.forget(key)  # running again (or never stopped)
        if not instance.state.is_billable_compute or max_lifetime_s is None:
            continue
        basis = instance.created_at
        if is_tracked and tracked.session_started_at:
            basis = tracked.session_started_at
        age = now - basis
        if age <= max_lifetime_s + SWEEP_GRACE_S:
            continue
        if is_tracked and _tracked_lock_active(tracked, allow_indefinite_lock, now):
            result.actions.append(
                SweepAction(
                    instance, policy.ACTION_NONE, REASON_LOCKED, tracked=True, age_seconds=age
                )
            )
            continue
        # Stopping keeps the tracked project's disk for a quick restart; an
        # instance nobody tracks has no one to restart it, so it goes entirely.
        if is_tracked and provider.capabilities.stop:
            action = policy.ACTION_STOP
        else:
            action = policy.ACTION_DESTROY
        result.actions.append(
            SweepAction(
                instance, action, REASON_ORPHAN_LIFETIME, tracked=is_tracked, age_seconds=age
            )
        )

    registry.retain(provider.name, present)

    for planned in result.actions:
        _describe(planned, apply, say)
        if not apply or planned.action == policy.ACTION_NONE:
            continue
        try:
            if planned.action == policy.ACTION_DESTROY:
                provider.destroy(planned.instance)
                registry.forget(registry.key(provider.name, planned.instance.id))
            else:
                provider.stop(planned.instance)
            planned.applied = True
        except ProviderError as exc:
            planned.error = str(exc)
            say(f"! {planned.action} {planned.instance.id} failed: {exc}")
        for sink in sinks:
            _emit(sink, planned)

    registry.save()
    return result


def _describe(planned: SweepAction, apply: bool, say) -> None:
    from .durations import format_duration

    if planned.action == policy.ACTION_NONE:
        say(
            f"= {planned.instance.id} ({planned.instance.label}) past max_lifetime "
            "but locked locally; left alone"
        )
        return
    if planned.stopped_seconds is not None:
        why = f"stopped for {format_duration(planned.stopped_seconds)}"
    else:
        why = f"running for {format_duration(planned.age_seconds)}"
    verb = planned.action if apply else f"would {planned.action}"
    tracked = ", tracked here" if planned.tracked else ""
    say(
        f"- {verb} {planned.instance.id} ({planned.instance.label}{tracked}): "
        f"{planned.reason}, {why}"
    )


def _emit(sink: EventLog, planned: SweepAction) -> None:
    detail = planned.to_dict()
    detail.pop("instance_id")
    sink.emit(SWEEP_ACTION, instance_id=planned.instance.id, source="sweep", **detail)
    if not planned.applied:
        return
    name = ev.INSTANCE_DESTROYED if planned.action == policy.ACTION_DESTROY else ev.INSTANCE_STOPPED
    sink.emit(name, instance_id=planned.instance.id, reason=planned.reason, source="sweep")


def account_rows(
    instances: list[Instance], tracked_id: Optional[str], now: Optional[float] = None
) -> list[dict[str, Any]]:
    now = now if now is not None else utc_now()
    return [
        {
            "instance_id": i.id,
            "label": i.label,
            "state": i.state.value,
            "gpu": f"{i.gpu_count}x {i.gpu_name}".strip() if i.gpu_name else "",
            "hourly_cost_usd": i.hourly_cost_usd,
            "created_at": i.created_at,
            "age_seconds": round(max(0.0, now - i.created_at), 1) if i.created_at else None,
            "tracked": tracked_id is not None and i.id == tracked_id,
        }
        for i in instances
    ]

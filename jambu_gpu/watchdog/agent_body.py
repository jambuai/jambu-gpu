"""Remote lifecycle watchdog (spec section 14).

This file is NOT imported at runtime by the CLI. Its source is concatenated
with ``core/policy.py`` and the provider-supplied action source into a single
stdlib-only script that runs *on the rented instance*, so lifecycle
enforcement survives the local CLI crashing, the laptop sleeping or the
network dying.

Contract with the CLI (all routes require ``X-Jambu-Token``):
    GET  /health              -> liveness
    GET  /state               -> snapshot + current policy decision
    GET  /events?limit=N      -> structured events recorded on the instance
    POST /activity            -> generic activity mark
    POST /ready               -> model server finished starting
    POST /workload/start      -> {id, label}
    POST /workload/heartbeat  -> {id}
    POST /workload/end        -> {id, returncode, state}
    POST /lock                -> {ttl_seconds|until|null}
    POST /unlock
    POST /shutdown            -> {action: stop|destroy, reason}
"""

# The generated script inlines core/policy.py above this body. When this file
# is imported directly (tests, linting) we pull the same names from the package.
if "evaluate" not in globals():  # pragma: no cover - only in standalone import
    from jambu_gpu.core.policy import (  # noqa: F401
        ACTION_DESTROY,
        ACTION_NONE,
        ACTION_STOP,
        Decision,
        PolicyConfig,
        Snapshot,
        Workload,
        derive_state,
        evaluate,
    )

import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG_PATH = os.environ.get("JAMBU_WATCHDOG_CONFIG", "/etc/jambu/watchdog.json")
STATE_PATH = os.environ.get("JAMBU_WATCHDOG_STATE", "/var/lib/jambu/activity.json")
EVENTS_PATH = os.environ.get("JAMBU_WATCHDOG_EVENTS", "/var/log/jambu/events.jsonl")

_LOCK = threading.RLock()
_STATE = {}
_CONFIG = {}
_STOPPED = threading.Event()


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------


def _atomic_write(path, payload):
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def log_event(event, **fields):
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        "source": "watchdog",
        "provider": _CONFIG.get("provider"),
        "instance_id": (_STATE or {}).get("instance_id") or _CONFIG.get("instance_id"),
    }
    record.update(fields)
    line = json.dumps(record, default=str)
    try:
        os.makedirs(os.path.dirname(EVENTS_PATH) or ".", exist_ok=True)
        with open(EVENTS_PATH, "a") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    print("[jambu-watchdog] " + line, flush=True)
    return record


def load_state():
    now = time.time()
    default = {
        "created_at": float(_CONFIG.get("created_at") or now),
        "last_activity": now,
        "starting": True,
        "locked": False,
        "locked_until": None,
        "workloads": {},
        "terminated": False,
    }
    try:
        with open(STATE_PATH) as handle:
            stored = json.load(handle)
        default.update(stored)
    except (OSError, ValueError):
        pass
    return default


def save_state():
    try:
        _atomic_write(STATE_PATH, json.dumps(_STATE, indent=2, default=str))
    except OSError:
        pass


def touch(reason="activity"):
    with _LOCK:
        _STATE["last_activity"] = time.time()
        save_state()
    return reason


# --------------------------------------------------------------------------
# policy wiring
# --------------------------------------------------------------------------


def policy_config():
    merged = dict(_CONFIG.get("policy") or {})
    merged.update(_STATE.get("policy_overrides") or {})
    return PolicyConfig.from_dict(merged)


def snapshot(now=None):
    now = now if now is not None else time.time()
    with _LOCK:
        workloads = [
            Workload(
                id=key,
                started_at=float(value.get("started_at") or now),
                last_heartbeat=float(value.get("last_heartbeat") or now),
                label=value.get("label", ""),
                state=value.get("state", "running"),
            )
            for key, value in (_STATE.get("workloads") or {}).items()
        ]
        starting = bool(_STATE.get("starting"))
        if starting:
            grace = float(_CONFIG.get("startup_grace_s") or 900.0)
            if now - float(_STATE.get("created_at") or now) > grace:
                # The CLI never confirmed readiness; stop pretending we booted.
                starting = False
                _STATE["starting"] = False
                log_event("runtime_startup_grace_expired", grace_seconds=grace)
                save_state()
        return Snapshot(
            now=now,
            created_at=float(_STATE.get("created_at") or now),
            last_activity=float(_STATE.get("last_activity") or now),
            workloads=workloads,
            starting=starting,
            locked=bool(_STATE.get("locked")),
            locked_until=_STATE.get("locked_until"),
            provider_state="ready",
        )


def build_context():
    ctx = dict(_CONFIG.get("provider_context") or {})
    ctx["instance_id"] = _STATE.get("instance_id") or _CONFIG.get("instance_id")
    return ctx


def terminate(action, reason, detail=None):
    detail = detail or {}
    with _LOCK:
        if _STATE.get("terminated"):
            return False
        _STATE["terminated"] = True
        save_state()

    log_event(
        "lifecycle_guard_triggered",
        action=action,
        reason=reason,
        **detail,
    )
    ctx = build_context()
    try:
        if action == ACTION_DESTROY:
            provider_destroy(ctx)  # noqa: F821 - injected by the builder
            log_event("instance_destroyed", reason=reason, **detail)
        else:
            provider_stop(ctx)  # noqa: F821 - injected by the builder
            log_event("instance_stopped", reason=reason, **detail)
    except Exception as exc:  # noqa: BLE001 - last line of defence
        log_event(
            "provider_error",
            action=action,
            reason=reason,
            error=str(exc),
            traceback=traceback.format_exc(limit=3),
        )
        with _LOCK:
            _STATE["terminated"] = False
            save_state()
        return False
    return True


def guard_loop():
    interval = float(_CONFIG.get("interval_s") or 60.0)
    cfg = policy_config()
    log_event(
        "watchdog_started",
        interval_seconds=interval,
        idle_timeout_seconds=cfg.idle_timeout_s,
        max_lifetime_seconds=cfg.max_lifetime_s,
        auto_stop=cfg.auto_stop,
        auto_destroy=cfg.auto_destroy,
    )
    idle_announced = False
    while not _STOPPED.wait(interval):
        try:
            cfg = policy_config()
            snap = snapshot()

            with _LOCK:
                if _STATE.get("locked") and snap.lock_expired(cfg):
                    _STATE["locked"] = False
                    _STATE["locked_until"] = None
                    save_state()
                    log_event("instance_unlocked", reason="lock_expired")

            decision = evaluate(snap, cfg)
            state = decision.detail.get("state")
            if state == "idle" and not idle_announced:
                idle_announced = True
                log_event("instance_idle", **decision.detail)
            elif state != "idle":
                idle_announced = False

            if decision.terminates:
                terminate(decision.action, decision.reason, decision.detail)
                _STOPPED.set()
        except Exception as exc:  # noqa: BLE001 - never let the guard die
            log_event("watchdog_error", error=str(exc), traceback=traceback.format_exc(limit=3))


# --------------------------------------------------------------------------
# HTTP control surface
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "jambu-watchdog/1.0"

    def log_message(self, fmt, *args):  # noqa: A003 - silence access logs
        return

    # -- plumbing -------------------------------------------------------

    def _authorized(self):
        expected = _CONFIG.get("token")
        if not expected:
            return True
        supplied = self.headers.get("X-Jambu-Token") or ""
        if supplied != expected:
            self._reply(401, {"error": "unauthorized"})
            return False
        return True

    def _reply(self, code, payload):
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _payload(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    # -- routes ---------------------------------------------------------

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/health":
            self._reply(200, {"ok": True, "service": "jambu-watchdog"})
            return
        if not self._authorized():
            return
        if path == "/state":
            self._reply(200, state_payload())
            return
        if path == "/events":
            limit = 100
            if "limit=" in self.path:
                try:
                    limit = int(self.path.split("limit=")[1].split("&")[0])
                except ValueError:
                    limit = 100
            self._reply(200, {"events": read_events(limit)})
            return
        self._reply(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        if not self._authorized():
            return
        body = self._payload()
        try:
            self._reply(*dispatch(path, body))
        except Exception as exc:  # noqa: BLE001
            log_event("watchdog_error", route=path, error=str(exc))
            self._reply(500, {"error": str(exc)})


def state_payload():
    cfg = policy_config()
    snap = snapshot()
    decision = evaluate(snap, cfg)
    with _LOCK:
        workloads = json.loads(json.dumps(_STATE.get("workloads") or {}, default=str))
        return {
            "instance_id": _STATE.get("instance_id") or _CONFIG.get("instance_id"),
            "provider": _CONFIG.get("provider"),
            "created_at": _STATE.get("created_at"),
            "last_activity": _STATE.get("last_activity"),
            "starting": snap.starting,
            "locked": snap.lock_active(cfg),
            "locked_until": _STATE.get("locked_until"),
            "terminated": bool(_STATE.get("terminated")),
            "state": derive_state(snap, cfg),
            "idle_seconds": round(snap.idle_seconds(cfg), 1),
            "age_seconds": round(snap.age_seconds(), 1),
            "workloads": workloads,
            "decision": decision.to_dict(),
            "policy": cfg.to_dict(),
            "now": time.time(),
        }


def read_events(limit=100):
    try:
        with open(EVENTS_PATH) as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def dispatch(path, body):
    now = time.time()

    if path == "/identify":
        # The instance id only exists after creation, and the onstart script is
        # baked before that; the CLI tells the watchdog who it is once it can.
        with _LOCK:
            if body.get("instance_id"):
                _STATE["instance_id"] = str(body["instance_id"])
            overrides = _STATE.setdefault("policy_overrides", {})
            if body.get("hourly_cost_usd") is not None:
                overrides["hourly_cost_usd"] = float(body["hourly_cost_usd"])
            if body.get("policy"):
                overrides.update(dict(body["policy"]))
            _STATE["last_activity"] = now
            save_state()
        log_event("watchdog_identified", instance_id=_STATE.get("instance_id"))
        return 200, {"ok": True, "instance_id": _STATE.get("instance_id")}

    if path == "/activity":
        touch()
        return 200, {"ok": True, "last_activity": _STATE["last_activity"]}

    if path == "/ready":
        with _LOCK:
            _STATE["starting"] = False
            _STATE["last_activity"] = now
            save_state()
        log_event("runtime_ready")
        return 200, {"ok": True}

    if path == "/starting":
        with _LOCK:
            _STATE["starting"] = True
            _STATE["created_at"] = _STATE.get("created_at") or now
            _STATE["last_activity"] = now
            save_state()
        log_event("runtime_starting")
        return 200, {"ok": True}

    if path == "/workload/start":
        workload_id = str(body.get("id") or "w-%d" % int(now * 1000))
        with _LOCK:
            _STATE.setdefault("workloads", {})[workload_id] = {
                "started_at": now,
                "last_heartbeat": now,
                "label": body.get("label", ""),
                "state": body.get("state", "running"),
            }
            _STATE["last_activity"] = now
            _STATE["starting"] = False
            save_state()
        log_event("workload_started", workload_id=workload_id, label=body.get("label", ""))
        return 200, {"ok": True, "id": workload_id}

    if path == "/workload/heartbeat":
        workload_id = body.get("id")
        with _LOCK:
            record = (_STATE.get("workloads") or {}).get(workload_id)
            if record is None and workload_id:
                record = {
                    "started_at": now,
                    "label": body.get("label", ""),
                    "state": "running",
                }
                _STATE.setdefault("workloads", {})[workload_id] = record
            if record is not None:
                record["last_heartbeat"] = now
            _STATE["last_activity"] = now
            save_state()
        return 200, {"ok": True, "id": workload_id}

    if path == "/workload/end":
        workload_id = body.get("id")
        with _LOCK:
            record = (_STATE.get("workloads") or {}).get(workload_id)
            if record is not None:
                record["state"] = body.get("state") or (
                    "finished" if not body.get("returncode") else "failed"
                )
                record["finished_at"] = now
                record["returncode"] = body.get("returncode")
            _STATE["last_activity"] = now
            save_state()
        log_event(
            "workload_finished",
            workload_id=workload_id,
            returncode=body.get("returncode"),
        )
        return 200, {"ok": True}

    if path == "/lock":
        ttl = body.get("ttl_seconds")
        until = body.get("until")
        cfg = policy_config()
        if until is None and ttl:
            until = now + float(ttl)
        if until is None and not cfg.allow_indefinite_lock:
            return 400, {
                "error": "indefinite locks are disabled "
                "(set lifecycle.allow_indefinite_lock: true to permit them)"
            }
        with _LOCK:
            _STATE["locked"] = True
            _STATE["locked_until"] = float(until) if until is not None else None
            _STATE["last_activity"] = now
            save_state()
        log_event("instance_locked", locked_until=until, reason=body.get("reason", ""))
        return 200, {"ok": True, "locked_until": until}

    if path == "/unlock":
        with _LOCK:
            _STATE["locked"] = False
            _STATE["locked_until"] = None
            _STATE["last_activity"] = now
            save_state()
        log_event("instance_unlocked", reason=body.get("reason", "manual"))
        return 200, {"ok": True}

    if path == "/shutdown":
        action = body.get("action") or ACTION_STOP
        reason = body.get("reason") or "manual"
        threading.Thread(
            target=terminate, args=(action, reason, {"requested_by": "cli"}), daemon=True
        ).start()
        return 202, {"ok": True, "action": action}

    return 404, {"error": "not found"}


# --------------------------------------------------------------------------
# entrypoint
# --------------------------------------------------------------------------


def main():
    global _CONFIG, _STATE

    try:
        with open(CONFIG_PATH) as handle:
            _CONFIG = json.load(handle)
    except (OSError, ValueError) as exc:
        print("[jambu-watchdog] cannot read %s: %s" % (CONFIG_PATH, exc), file=sys.stderr)
        return 1

    for key, value in (_CONFIG.get("env") or {}).items():
        os.environ.setdefault(str(key), str(value))

    _STATE = load_state()
    save_state()

    port = int(_CONFIG.get("port") or 8777)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True

    guard = threading.Thread(target=guard_loop, daemon=True)
    guard.start()

    log_event("watchdog_listening", port=port)
    try:
        server.serve_forever(poll_interval=1.0)
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        _STOPPED.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

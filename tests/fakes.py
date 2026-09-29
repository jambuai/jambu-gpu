"""An in-memory provider used to exercise the core without touching a network."""

from __future__ import annotations

import time
from typing import Optional

from jambu_gpu.core.errors import CapacityUnavailableError, ProvisioningError
from jambu_gpu.core.models import (
    ComputeSpec,
    Endpoint,
    Execution,
    Instance,
    InstanceState,
    InstanceStatus,
    LogStream,
    Offer,
    ProviderCapabilities,
    ValidationResult,
    Workload,
)
from jambu_gpu.providers.base import GPUProvider, WatchdogSpec

FAKE_ACTIONS = '''
def provider_stop(ctx):
    return None


def provider_destroy(ctx):
    return None
'''


class FakeProvider(GPUProvider):
    name = "fake"
    display_name = "Fake"
    required_credentials: list[str] = []
    capabilities = ProviderCapabilities(
        stop=True,
        destroy=True,
        ssh=True,
        public_ports=True,
        gpu_selection=True,
        pricing_query=True,
        remote_exec=True,
        logs=True,
        spot_instances=True,
        stop_eliminates_billing=True,
    )

    def __init__(self, config, credentials) -> None:
        super().__init__(config, credentials)
        self.instances: dict[str, Instance] = {}
        self.counter = 0
        self.calls: list[tuple[str, str]] = []
        self.offer_price = 0.42
        self.fail_setup: Optional[Exception] = None
        self.no_offers = False

    # -- contract -------------------------------------------------------

    def validate(self, config) -> ValidationResult:
        return ValidationResult()

    def find_offers(self, spec: ComputeSpec, limit: int = 10) -> list[Offer]:
        if self.no_offers:
            return []
        return [
            Offer(
                id="offer-1",
                gpu_name="RTX 4090",
                gpu_count=spec.gpu.count,
                gpu_vram_gb=float(spec.gpu.min_vram_gb),
                disk_gb=float(spec.disk_gb),
                hourly_cost_usd=self.offer_price,
            )
        ]

    def setup(self, spec: ComputeSpec) -> Instance:
        if self.fail_setup is not None:
            self.counter += 1
            instance_id = f"i-{self.counter}"
            self.instances[instance_id] = Instance(
                id=instance_id, provider=self.name, state=InstanceState.PROVISIONING
            )
            raise self.fail_setup
        if self.no_offers:
            raise CapacityUnavailableError("no capacity")
        self.counter += 1
        instance_id = f"i-{self.counter}"
        instance = Instance(
            id=instance_id,
            provider=self.name,
            state=InstanceState.READY,
            created_at=time.time(),
            gpu_name="RTX 4090",
            gpu_count=spec.gpu.count,
            hourly_cost_usd=self.offer_price,
            host="127.0.0.1",
            ssh_host="ssh.fake",
            ssh_port=2222,
            endpoints={
                "model": Endpoint("model", "http://127.0.0.1:9", 8000, 9),
                "watchdog": Endpoint("watchdog", "http://127.0.0.1:9", 8777, 9),
            },
            label=spec.label,
        )
        self.instances[instance_id] = instance
        self.calls.append(("setup", instance_id))
        return instance

    def start(self, instance: Instance) -> Instance:
        stored = self.instances.get(instance.id)
        if stored is None:
            raise ProvisioningError("gone")
        stored.state = InstanceState.READY
        self.calls.append(("start", instance.id))
        return stored

    def run(self, instance: Instance, workload: Workload) -> Execution:
        self.calls.append(("run", instance.id))
        return Execution(
            id="e1",
            workload_id=workload.id,
            started_at=time.time(),
            finished_at=time.time(),
            returncode=0,
            state="finished",
        )

    def status(self, instance: Instance) -> InstanceStatus:
        stored = self.instances.get(instance.id)
        if stored is None:
            return InstanceStatus(state=InstanceState.DESTROYED, exists=False)
        return InstanceStatus(
            state=stored.state,
            instance=stored,
            exists=True,
            hourly_cost_usd=stored.hourly_cost_usd,
        )

    def stop(self, instance: Instance) -> None:
        self.calls.append(("stop", instance.id))
        stored = self.instances.get(instance.id)
        if stored is not None:
            stored.state = InstanceState.STOPPED

    def destroy(self, instance: Instance) -> None:
        self.calls.append(("destroy", instance.id))
        self.instances.pop(instance.id, None)

    def logs(self, instance: Instance) -> LogStream:
        return LogStream(iter(["line one", "line two"]), source="fake")

    def watchdog_spec(self, spec_or_instance=None) -> WatchdogSpec:
        return WatchdogSpec(actions_source=FAKE_ACTIONS, env={}, context={})

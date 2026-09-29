"""Vast-specific translation. Nothing Vast-shaped may escape the adapter."""

from __future__ import annotations

from jambu_gpu.core.models import ComputeSpec, GpuSpec, InstanceState
from jambu_gpu.providers.vast import mapper
from jambu_gpu.providers.vast.config import VastOptions

RUNNING = {
    "id": 19384723,
    "actual_status": "running",
    "intended_status": "running",
    "gpu_name": "RTX 4090",
    "num_gpus": 1,
    "gpu_ram": 24564,
    "disk_space": 80.0,
    "dph_total": 0.412,
    "public_ipaddr": "1.2.3.4",
    "ssh_host": "ssh5.vast.ai",
    "ssh_port": 41234,
    "start_date": 1_700_000_000.0,
    "label": "jambu-test",
    "ports": {
        "8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "41000"}],
        "8777/tcp": [{"HostIp": "0.0.0.0", "HostPort": "41777"}],
    },
}

WANTED = {8000: "model", 8777: "watchdog"}


def test_state_mapping_covers_the_vast_vocabulary():
    assert mapper.map_state({"actual_status": "running"}) is InstanceState.READY
    assert mapper.map_state({"actual_status": "loading"}) is InstanceState.STARTING
    assert mapper.map_state({"actual_status": "created"}) is InstanceState.PROVISIONING
    assert mapper.map_state({"actual_status": "exited"}) is InstanceState.STOPPED
    assert mapper.map_state({"actual_status": "offline"}) is InstanceState.UNKNOWN
    assert mapper.map_state({"status_msg": "error: image pull failed"}) is InstanceState.FAILED
    assert mapper.map_state({}) is InstanceState.UNKNOWN


def test_running_but_not_yet_networked_is_still_starting():
    raw = dict(RUNNING, public_ipaddr=None)
    assert mapper.map_state(raw) is InstanceState.STARTING


def test_vram_units_are_normalized():
    assert mapper.vram_gb(24564) == 24.0     # MB, the usual Vast shape
    assert mapper.vram_gb(24) == 24.0        # already GB
    assert mapper.vram_gb(None) == 0.0


def test_instance_mapping_publishes_endpoints():
    instance = mapper.map_instance(RUNNING, WANTED)
    assert instance.id == "19384723"
    assert instance.provider == "vast"
    assert instance.state is InstanceState.READY
    assert instance.endpoint_url("model") == "http://1.2.3.4:41000"
    assert instance.endpoint_url("watchdog") == "http://1.2.3.4:41777"
    assert instance.ssh_port == 41234
    assert instance.hourly_cost_usd == 0.412


def test_unpublished_ports_are_simply_absent():
    instance = mapper.map_instance(dict(RUNNING, ports={}), WANTED)
    assert instance.endpoints == {}


def test_search_query_translates_the_compute_spec():
    spec = ComputeSpec(
        gpu=GpuSpec(min_vram_gb=48, count=2, name_filter="A100"),
        disk_gb=120,
        max_hourly_cost_usd=2.5,
    )
    query = mapper.build_search_query(spec, VastOptions())
    assert query["num_gpus"] == {"eq": 2}
    assert query["gpu_ram"] == {"gte": 48 * 1024.0}
    assert query["disk_space"] == {"gte": 120.0}
    assert query["dph_total"] == {"lte": 2.5}
    # gpu_name is deliberately NOT sent server-side (Vast only does exact
    # match there; offer_matches below does the real substring filtering).
    assert "gpu_name" not in query
    assert query["type"] == "on-demand"


def test_spot_requests_a_bid_ask():
    spec = ComputeSpec(gpu=GpuSpec(), spot=True)
    assert mapper.build_search_query(spec, VastOptions())["type"] == "bid"


def test_offers_are_rechecked_client_side():
    spec = ComputeSpec(gpu=GpuSpec(min_vram_gb=48, count=1), disk_gb=80)
    too_small = mapper.map_offer(
        {"id": 1, "gpu_ram": 24564, "num_gpus": 1, "disk_space": 80, "dph_total": 0.1}
    )
    big_enough = mapper.map_offer(
        {"id": 2, "gpu_ram": 49152, "num_gpus": 1, "disk_space": 80, "dph_total": 0.9}
    )
    assert not mapper.offer_matches(too_small, spec)
    assert mapper.offer_matches(big_enough, spec)


def test_create_payload_publishes_ports_the_vast_way():
    spec = ComputeSpec(gpu=GpuSpec(), disk_gb=80, image="vllm/vllm-openai:latest", label="x")
    payload = mapper.build_create_payload(
        spec, VastOptions(), [8000, 8777], "#!/bin/bash\necho hi"
    )
    assert payload["image"] == "vllm/vllm-openai:latest"
    assert payload["env"]["-p 8000:8000"] == "1"
    assert payload["env"]["-p 8777:8777"] == "1"
    assert payload["target_state"] == "running"
    assert payload["onstart"].startswith("#!/bin/bash")


def test_unknown_provider_options_are_rejected_early():
    import pytest

    with pytest.raises(ValueError):
        VastOptions.from_mapping({"nonsense": 1})

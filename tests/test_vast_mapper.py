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


# -- CUDA: the image's floor and the adapter's floor, whichever is higher -----


def _spec(cuda_min=None):
    return ComputeSpec(gpu=GpuSpec(min_vram_gb=24, cuda_min=cuda_min), disk_gb=80)


def _offer(cuda):
    raw = {"id": 1, "gpu_name": "RTX 4090", "num_gpus": 1, "gpu_ram": 24564,
           "disk_space": 100, "dph_total": 0.3, "cuda_max_good": cuda}
    return mapper.map_offer(raw)


def test_offers_carry_the_hosts_cuda_ceiling():
    assert _offer(12.2).cuda_max == 12.2
    assert _offer(None).cuda_max is None


def test_the_search_asks_for_the_stricter_cuda_floor():
    options = VastOptions()  # min_cuda 12.1
    assert mapper.build_search_query(_spec(), options)["cuda_max_good"] == {"gte": 12.1}
    assert mapper.build_search_query(_spec("12.9"), options)["cuda_max_good"] == {"gte": 12.9}
    strict = VastOptions(min_cuda=13.0)
    assert mapper.build_search_query(_spec("12.9"), strict)["cuda_max_good"] == {"gte": 13.0}
    loose = VastOptions(min_cuda=0)
    assert "cuda_max_good" not in mapper.build_search_query(_spec(), loose)


def test_a_host_whose_driver_is_too_old_for_the_image_is_rejected_client_side():
    """Caught on a live run: a CUDA 13 image landed on a 12.2 driver and died."""
    spec = _spec("12.9")
    assert not mapper.offer_matches(_offer(12.2), spec)
    assert mapper.offer_matches(_offer(12.9), spec)
    assert mapper.offer_matches(_offer(13.0), spec)
    assert mapper.offer_matches(_offer(None), spec)  # unknown: trust the server filter
    # the adapter's own floor counts too
    assert not mapper.offer_matches(_offer(12.9), spec, min_cuda=13.0)
    assert mapper.offer_matches(_offer(12.0), _spec())


def test_find_offers_applies_the_effective_floor(monkeypatch):
    from jambu_gpu.providers.vast.provider import VastProvider

    provider = VastProvider.__new__(VastProvider)
    provider.options = VastOptions(min_cuda=12.1)
    seen = {}

    class _Client:
        def search_offers(self, query):
            seen["query"] = query
            return [
                {"id": 1, "gpu_name": "A", "num_gpus": 1, "gpu_ram": 24564,
                 "disk_space": 100, "dph_total": 0.2, "cuda_max_good": 12.2},
                {"id": 2, "gpu_name": "B", "num_gpus": 1, "gpu_ram": 24564,
                 "disk_space": 100, "dph_total": 0.3, "cuda_max_good": 12.9},
            ]

    monkeypatch.setattr(VastProvider, "client", property(lambda self: _Client()))
    offers = provider.find_offers(_spec("12.9"))
    assert [o.id for o in offers] == ["2"]
    assert seen["query"]["cuda_max_good"] == {"gte": 12.9}

"""The Vast adapter against a mocked Vast API."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from jambu_gpu.core.credentials import CredentialResolver
from jambu_gpu.core.errors import (
    AuthenticationError,
    CapacityUnavailableError,
    UnsupportedCapabilityError,
)
from jambu_gpu.core.models import InstanceState
from jambu_gpu.providers.vast.config import API_BASE
from jambu_gpu.providers.vast.provider import VastProvider

OFFER = {
    "id": 777,
    "gpu_name": "RTX 4090",
    "num_gpus": 1,
    "gpu_ram": 24564,
    "disk_space": 200.0,
    "dph_total": 0.35,
    "geolocation": "Poland",
    "reliability2": 0.99,
}

INSTANCE = {
    "id": 19384723,
    "actual_status": "running",
    "intended_status": "running",
    "gpu_name": "RTX 4090",
    "num_gpus": 1,
    "gpu_ram": 24564,
    "dph_total": 0.35,
    "public_ipaddr": "1.2.3.4",
    "ssh_host": "ssh5.vast.ai",
    "ssh_port": 41234,
    "start_date": 1_700_000_000.0,
    "ports": {
        "8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "41000"}],
        "8777/tcp": [{"HostIp": "0.0.0.0", "HostPort": "41777"}],
    },
}


@pytest.fixture
def provider(config, monkeypatch):
    monkeypatch.setenv("VAST_API_KEY", "test-key")
    return VastProvider(config, CredentialResolver(config.project_dir))


@respx.mock
def test_offers_are_discovered_and_normalized(provider):
    respx.put(f"{API_BASE}/search/asks/").mock(
        return_value=httpx.Response(200, json={"offers": [OFFER]})
    )
    offers = provider.find_offers(provider.build_spec_preview(provider.config))
    assert len(offers) == 1
    assert offers[0].gpu_vram_gb == 24.0
    assert offers[0].hourly_cost_usd == 0.35


@respx.mock
def test_setup_creates_an_instance_from_the_cheapest_offer(provider):
    respx.put(f"{API_BASE}/search/asks/").mock(
        return_value=httpx.Response(200, json={"offers": [OFFER]})
    )
    create = respx.put(f"{API_BASE}/asks/777/").mock(
        return_value=httpx.Response(200, json={"success": True, "new_contract": 19384723})
    )
    respx.get(f"{API_BASE}/instances/19384723/").mock(
        return_value=httpx.Response(200, json={"instances": INSTANCE})
    )

    spec = provider.build_spec_preview(provider.config)
    spec.image = "vllm/vllm-openai:latest"
    spec.onstart = "#!/bin/bash\necho hi"
    instance = provider.setup(spec)

    assert instance.id == "19384723"
    assert instance.state is InstanceState.READY
    assert instance.endpoint_url("model") == "http://1.2.3.4:41000"
    payload = create.calls[0].request.content.decode()
    assert "vllm/vllm-openai:latest" in payload
    assert "-p 8000:8000" in payload


@respx.mock
def test_setup_walks_past_offers_that_were_taken(provider):
    second = dict(OFFER, id=888, dph_total=0.55)
    respx.put(f"{API_BASE}/search/asks/").mock(
        return_value=httpx.Response(200, json={"offers": [OFFER, second]})
    )
    respx.put(f"{API_BASE}/asks/777/").mock(
        return_value=httpx.Response(200, json={"success": False, "msg": "no such ask"})
    )
    respx.put(f"{API_BASE}/asks/888/").mock(
        return_value=httpx.Response(200, json={"success": True, "new_contract": 19384723})
    )
    respx.get(f"{API_BASE}/instances/19384723/").mock(
        return_value=httpx.Response(200, json={"instances": INSTANCE})
    )
    assert provider.setup(provider.build_spec_preview(provider.config)).id == "19384723"


@respx.mock
def test_no_capacity_is_a_normalized_error(provider):
    respx.put(f"{API_BASE}/search/asks/").mock(
        return_value=httpx.Response(200, json={"offers": []})
    )
    respx.get(f"{API_BASE}/bundles/").mock(
        return_value=httpx.Response(200, json={"offers": []})
    )
    with pytest.raises(CapacityUnavailableError):
        provider.setup(provider.build_spec_preview(provider.config))


@respx.mock
def test_a_bad_key_is_a_normalized_error(provider):
    respx.get(f"{API_BASE}/users/current/").mock(return_value=httpx.Response(401))
    with pytest.raises(AuthenticationError):
        provider.client.whoami()


@respx.mock
def test_status_reports_destroyed_when_the_instance_is_gone(provider):
    respx.get(f"{API_BASE}/instances/19384723/").mock(return_value=httpx.Response(404))
    respx.get(f"{API_BASE}/instances/").mock(
        return_value=httpx.Response(200, json={"instances": []})
    )
    from jambu_gpu.core.models import Instance

    status = provider.status(Instance(id="19384723", provider="vast"))
    assert status.exists is False
    assert status.state is InstanceState.DESTROYED


@respx.mock
def test_stop_and_destroy_are_idempotent(provider):
    from jambu_gpu.core.models import Instance

    instance = Instance(id="19384723", provider="vast")
    respx.put(f"{API_BASE}/instances/19384723/").mock(return_value=httpx.Response(404))
    respx.delete(f"{API_BASE}/instances/19384723/").mock(return_value=httpx.Response(404))
    provider.stop(instance)     # must not raise
    provider.destroy(instance)  # must not raise


@respx.mock
def test_validate_fails_before_provisioning_without_a_key(config):
    provider = VastProvider(config, CredentialResolver(config.project_dir))
    provider.credentials.sources = []
    import os

    os.environ.pop("VAST_API_KEY", None)
    result = provider.validate(config)
    assert not result.ok
    assert "VAST_API_KEY" in result.errors[0].message
    assert not respx.calls  # no API call was attempted


def test_watchdog_actions_are_self_contained(provider):
    spec = provider.watchdog_spec(None)
    assert spec is not None
    compile(spec.actions_source, "actions.py", "exec")
    assert spec.env["VAST_API_KEY"] == "test-key"
    assert "import jambu" not in spec.actions_source


def test_unsupported_capability_is_explicit(provider):
    provider.capabilities.remote_exec = False
    try:
        with pytest.raises(UnsupportedCapabilityError):
            provider.require_capability("remote_exec")
    finally:
        provider.capabilities.remote_exec = True


@respx.mock
def test_search_falls_back_across_api_shapes(provider):
    respx.put(f"{API_BASE}/search/asks/").mock(return_value=httpx.Response(500))
    bundles = respx.get(f"{API_BASE}/bundles/").mock(
        return_value=httpx.Response(200, json={"offers": [OFFER]})
    )
    offers = provider.find_offers(provider.build_spec_preview(provider.config))
    assert len(offers) == 1 and bundles.called


@respx.mock
def test_a_bad_key_during_search_surfaces_as_authentication_error(provider):
    respx.put(f"{API_BASE}/search/asks/").mock(return_value=httpx.Response(401))
    respx.get(f"{API_BASE}/bundles/").mock(return_value=httpx.Response(401))
    with pytest.raises(AuthenticationError):
        provider.find_offers(provider.build_spec_preview(provider.config))


@respx.mock
def test_a_legitimate_empty_result_does_not_fall_through_to_a_broken_shape(provider):
    """Regression: a real 0-offers answer from the wrapped shape must not be
    masked by an error from the (never-going-to-work) bare/bundles fallbacks -
    this is exactly what happened live: name_filter genuinely matched nothing,
    and the old code surfaced the bare shape's 400 instead of "no offers".
    """
    calls = []

    def _respond(request):
        body = json.loads(request.content)
        calls.append(body)
        if "q" in body:  # correctly-wrapped shape: the real, working contract
            return httpx.Response(200, json={"offers": []})
        # bare shape: what Vast's current API actually rejects
        return httpx.Response(400, json={"success": False, "msg": "bad_request"})

    respx.put(f"{API_BASE}/search/asks/").mock(side_effect=_respond)
    offers = provider.client.search_offers({"gpu_ram": {"gte": 24564.0}})
    assert offers == []
    assert len(calls) == 1, "must not fall through to the bare shape after a valid empty answer"


# -- error classification survives real-world message spelling variance
# (found live: a genuine 400 "no_such_ask ... is not available" response
# was NOT recognized as capacity-unavailable, so setup()'s offer-retry loop
# never got a chance to try the next candidate and the whole run aborted) --


@respx.mock
def test_underscored_no_such_ask_on_a_400_is_capacity_unavailable(provider):
    respx.put(f"{API_BASE}/asks/999/").mock(
        return_value=httpx.Response(
            400,
            json={
                "success": False,
                "error": "invalid_args",
                "msg": "error 404/3603: no_such_ask Instance type by id 999 is not available.",
                "ask_id": 999,
            },
        )
    )
    with pytest.raises(CapacityUnavailableError):
        provider.client.request("PUT", "/asks/999/", json_body={})


@respx.mock
def test_setup_walks_to_the_next_offer_on_a_real_no_such_ask_400(provider):
    """The exact live failure: offer 777 went stale between search and
    purchase (a real Vast 400, not the earlier success:false-on-200 case) -
    setup() must still fall through to the next candidate, not abort.
    """
    second = dict(OFFER, id=888, dph_total=0.55)
    respx.put(f"{API_BASE}/search/asks/").mock(
        return_value=httpx.Response(200, json={"offers": [OFFER, second]})
    )
    respx.put(f"{API_BASE}/asks/777/").mock(
        return_value=httpx.Response(
            400,
            json={
                "success": False,
                "msg": "error 404/3603: no_such_ask Instance type by id 777 is not available.",
            },
        )
    )
    respx.put(f"{API_BASE}/asks/888/").mock(
        return_value=httpx.Response(200, json={"success": True, "new_contract": 19384723})
    )
    respx.get(f"{API_BASE}/instances/19384723/").mock(
        return_value=httpx.Response(200, json={"instances": INSTANCE})
    )
    assert provider.setup(provider.build_spec_preview(provider.config)).id == "19384723"


@respx.mock
def test_a_bad_key_on_a_401_is_still_authentication_error_regardless_of_body(provider):
    respx.get(f"{API_BASE}/users/current/").mock(
        return_value=httpx.Response(401, json={"success": False, "msg": "unauthorized"})
    )
    with pytest.raises(AuthenticationError):
        provider.client.whoami()


@respx.mock
def test_a_404_is_still_vastnotfound_even_with_a_success_false_body(provider):
    from jambu_gpu.core.models import Instance
    from jambu_gpu.providers.vast.client import VastNotFound

    respx.get(f"{API_BASE}/instances/123/").mock(
        return_value=httpx.Response(404, json={"success": False, "msg": "not found"})
    )
    respx.get(f"{API_BASE}/instances/").mock(
        return_value=httpx.Response(200, json={"instances": []})
    )
    # get_instance() catches VastNotFound internally and returns None - it
    # must not see a generic ProviderError instead.
    assert provider.status(Instance(id="123", provider="vast")).exists is False

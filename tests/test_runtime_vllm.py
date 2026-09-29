"""vLLM already speaks both OpenAI agent-facing APIs; this locks down that
jambu-gpu turns on the right flags for it and builds nothing of its own.
"""

from __future__ import annotations

import pytest

from jambu_gpu.runtime.vllm import VllmRuntime, guess_tool_call_parser


@pytest.mark.parametrize(
    "model_id,parser",
    [
        ("meta-llama/Llama-3.1-8B-Instruct", "llama3_json"),
        ("meta-llama/Llama-4-Scout-17B", "llama4_pythonic"),
        ("NousResearch/Hermes-3-Llama-3.1-8B", "llama3_json"),  # llama3 wins first
        ("Qwen/Qwen2.5-7B-Instruct", "hermes"),
        ("Qwen/Qwen3-Coder-30B", "qwen3_xml"),
        ("mistralai/Mistral-7B-Instruct-v0.3", "mistral"),
        ("ibm-granite/granite-4.0-tiny", "granite4"),
        ("ibm-granite/granite-3.1-8b-instruct", "granite"),
        ("deepseek-ai/DeepSeek-V3.1", "deepseek_v31"),
        ("deepseek-ai/DeepSeek-V3", "deepseek_v3"),
        ("Qwen/Qwen3-8B", "hermes"),  # original dash-versioned Qwen3: hermes
        # Qwen3.5+ point releases: XML function-call format (qwen3_xml), not
        # hermes - confirmed empirically against empero-ai/Qwythos-9B-v2.
        ("Qwen/Qwen3.8-27B", "qwen3_xml"),
        ("JonathanColetti/Qwen3.8-27B-Uncensored", "qwen3_xml"),
        ("huihui-ai/Huihui-Qwen3.8-27B-abliterated", "qwen3_xml"),
        ("empero-ai/Qwythos-9B-v2", None),  # unknown fine-tune (no "qwen3" in the name): no guess
    ],
)
def test_guess_tool_call_parser(model_id, parser):
    assert guess_tool_call_parser(model_id) == parser


def test_auto_enables_tool_calling_for_a_recognized_model(write_config):
    config = write_config(model={"id": "Qwen/Qwen2.5-7B-Instruct"})
    runtime = VllmRuntime(config)
    args = runtime.server_args()
    assert "--enable-auto-tool-choice" in args
    assert args[args.index("--tool-call-parser") + 1] == "hermes"


def test_auto_leaves_tool_calling_off_for_an_unknown_model(config):
    runtime = VllmRuntime(config)  # config fixture model.id is a fine-tune
    args = runtime.server_args()
    assert "--enable-auto-tool-choice" not in args
    assert "--tool-call-parser" not in args


def test_explicit_parser_overrides_the_guess(write_config):
    config = write_config(
        model={"id": "empero-ai/Qwythos-9B-v2"},
        runtime={"tool_call_parser": "hermes"},
    )
    runtime = VllmRuntime(config)
    args = runtime.server_args()
    assert args[args.index("--tool-call-parser") + 1] == "hermes"


def test_off_never_passes_tool_flags_even_for_a_recognized_model(write_config):
    config = write_config(
        model={"id": "Qwen/Qwen2.5-7B-Instruct"}, runtime={"tool_calling": "off"}
    )
    runtime = VllmRuntime(config)
    args = runtime.server_args()
    assert "--enable-auto-tool-choice" not in args
    assert "--tool-call-parser" not in args


def test_tool_server_is_passed_through_for_mcp_routing(write_config):
    config = write_config(
        model={"id": "Qwen/Qwen2.5-7B-Instruct"},
        runtime={"tool_server": "https://mcp.example.com"},
    )
    runtime = VllmRuntime(config)
    args = runtime.server_args()
    assert args[args.index("--tool-server") + 1] == "https://mcp.example.com"


def test_on_without_a_resolvable_parser_is_a_validation_error(config):
    config.runtime.tool_calling = "on"
    result = VllmRuntime(config).validate()
    assert not result.ok
    assert any("tool_call_parser" in (i.field or "") for i in result.errors)


def test_auto_without_a_resolvable_parser_is_only_a_warning(config):
    result = VllmRuntime(config).validate()  # default model.id is unrecognized
    assert result.ok
    assert any("tool_call_parser" in (i.field or "") for i in result.warnings)


def test_recognized_model_validates_clean(write_config):
    config = write_config(model={"id": "Qwen/Qwen2.5-7B-Instruct"})
    result = VllmRuntime(config).validate()
    assert not any("tool_call_parser" in (i.field or "") for i in result.issues)


def test_describe_surfaces_the_active_parser(write_config):
    config = write_config(model={"id": "Qwen/Qwen2.5-7B-Instruct"})
    assert "tools=hermes" in VllmRuntime(config).describe()


def test_qwythos_needs_an_explicit_override_since_its_name_hides_the_family():
    """Qwythos is Qwen3.5-based but the fine-tune name doesn't say so - auto
    can't guess it, confirmed live that qwen3_xml is the right parser for it.
    """
    assert guess_tool_call_parser("empero-ai/Qwythos-9B-v2") is None


# -- runtime.vllm_args: structured passthrough for un-modeled vLLM flags ----

from jambu_gpu.runtime.vllm import flatten_vllm_args


def test_flatten_vllm_args_covers_every_value_shape():
    args = flatten_vllm_args(
        {
            "max_num_seqs": 64,
            "enable-prefix-caching": True,
            "enforce_eager": False,
            "limit-mm-per-prompt": {"image": 0, "video": 0},
            "compilation-config": [1, 2, 4],
            "skip-this": None,
            "--already-dashed": "kept",
        }
    )
    assert args == [
        "--max-num-seqs", "64",
        "--enable-prefix-caching",
        "--no-enforce-eager",
        "--limit-mm-per-prompt", '{"image": 0, "video": 0}',
        "--compilation-config", "[1, 2, 4]",
        "--already-dashed", "kept",
    ]


def test_vllm_args_are_appended_to_server_args(write_config):
    config = write_config(runtime={"vllm_args": {"max_num_seqs": 32}})
    args = VllmRuntime(config).server_args()
    assert args[-2:] == ["--max-num-seqs", "32"]


def test_extra_args_win_over_vllm_args_on_conflict(write_config):
    """vllm_args, then extra_args - argparse takes the last occurrence."""
    config = write_config(
        runtime={
            "vllm_args": {"max_num_seqs": 32},
            "extra_args": ["--max-num-seqs", "64"],
        }
    )
    args = VllmRuntime(config).server_args()
    assert args[-2:] == ["--max-num-seqs", "64"]


def test_validate_warns_when_vllm_args_shadows_a_managed_flag(write_config):
    config = write_config(runtime={"vllm_args": {"port": 9000}})
    result = VllmRuntime(config).validate()
    assert any("vllm_args" in (i.field or "") for i in result.warnings)


def test_validate_is_quiet_for_unmanaged_vllm_args(write_config):
    config = write_config(runtime={"vllm_args": {"max_num_seqs": 32}})
    result = VllmRuntime(config).validate()
    assert not any("vllm_args" in (i.field or "") for i in result.issues)

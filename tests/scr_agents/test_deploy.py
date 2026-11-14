from pathlib import Path

import yaml

from scr_agents.config import Settings

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def test_compose_layout_uses_all_six_gpus_for_two_models():
    """Compose gives two offline vLLM servers all six GPUs on an internal network."""
    compose = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())
    services = compose["services"]
    assert set(services) == {"vllm-reviewer", "vllm-validator", "scr-agents"}
    gpus = []
    for name in ("vllm-reviewer", "vllm-validator"):
        devices = services[name]["deploy"]["resources"]["reservations"]["devices"][0]
        gpus += devices["device_ids"]
        assert services[name]["environment"]["HF_HUB_OFFLINE"] == "1"
        assert "--enable-auto-tool-choice" in services[name]["command"]
    assert sorted(gpus) == ["0", "1", "2", "3", "4", "5"] and len(set(gpus)) == 6
    assert "--tensor-parallel-size 2" in services["vllm-reviewer"]["command"]
    assert "--tensor-parallel-size 4" in services["vllm-validator"]["command"]
    assert compose["networks"]["internal"]["internal"] is True
    cc = services["scr-agents"]
    assert cc["cap_drop"] == ["ALL"] and cc["ports"] == ["8765:8765"]


def test_server_configs_validate_and_use_two_models():
    """Both server configs load, name two distinct models and allow no shell."""
    for name in ("scr_agents.yaml", "scr_agents.baremetal.yaml"):
        s = Settings.load(DEPLOY / name)
        assert s.llm.mode == "local"
        assert s.llm.local.model != s.llm.local.validator_model
        assert s.llm.local.base_url != s.llm.local.validator_base_url
        assert s.llm.parallel_validations >= 3 and s.llm.max_findings_to_validate >= 20
        assert (
            "sh" not in s.shell.allowed_binaries
            and "bash" not in s.shell.allowed_binaries
        )
    assert (DEPLOY / "Dockerfile").exists()
    assert (DEPLOY / "start_vllm.sh").stat().st_mode & 0o111

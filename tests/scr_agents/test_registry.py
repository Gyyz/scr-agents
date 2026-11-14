import pytest

from scr_agents.ws4_guardrails.registry import PromptRegistry, RegistryError


def test_sign_load_and_tamper_detection(tmp_path):
    """Prompts load only signed and approved, fail once edited, and count outcomes."""
    prompt = tmp_path / "p.md"
    prompt.write_text("be careful")
    reg = PromptRegistry(
        tmp_path / "manifest.yaml", stats_path=tmp_path / "stats.jsonl"
    )
    with pytest.raises(RegistryError):
        reg.load("p")
    reg.sign("p", prompt, approve=False)
    with pytest.raises(RegistryError):
        PromptRegistry(tmp_path / "manifest.yaml").load(
            "p"
        )  # registered but not approved
    reg.sign("p", prompt, approve=True)
    assert PromptRegistry(tmp_path / "manifest.yaml").load("p") == "be careful"
    prompt.write_text("be careless")
    with pytest.raises(RegistryError, match="hash mismatch"):
        PromptRegistry(tmp_path / "manifest.yaml").load("p")
    reg.record_outcome("p", True, "run-1", "F-1")
    reg.record_outcome("p", False, "run-1", "F-2")
    assert reg.stats()["p@A"] == {"accepted": 1, "rejected": 1}


def test_packaged_prompts_are_signed_and_approved():
    """Every packaged agent prompt loads from the shipped manifest."""
    from scr_agents.config import PROMPTS_DIR

    reg = PromptRegistry(PROMPTS_DIR / "manifest.yaml")
    for name in (
        "review_planner",
        "subagent_bugs",
        "subagent_vulnerabilities",
        "subagent_style",
        "validator",
    ):
        assert reg.load(name)

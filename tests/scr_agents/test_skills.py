"""Skills: operator-authored review guidance, loaded on demand by the agents.

Each skill is a skills/<name>/SKILL.md file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scr_agents import cli
from scr_agents.config import SKILLS_DIR, Settings
from scr_agents.models import Finding, Location, ReviewPlan
from scr_agents.pipeline import run_review
from scr_agents.ws1_review.lead import lead_tools
from scr_agents.ws1_review.subagents import SUBAGENTS, build_user_message
from scr_agents.ws1_review.tools import review_tools
from scr_agents.ws1_review.validator import ValidatorAgent
from scr_agents.ws4_guardrails.safety_filter import SafetyFilter
from scr_agents.ws5_knowledge.activity_log import ActivityLog, NullLog
from scr_agents.ws5_knowledge.skills import SkillLibrary

REPO_SKILLS = SKILLS_DIR


def _skill(root: Path, name: str, body: str, frontmatter: str | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True)
    text = (f"---\n{frontmatter}\n---\n" if frontmatter is not None else "") + body
    (d / "SKILL.md").write_text(text)
    return d / "SKILL.md"


def test_library_parses_frontmatter_and_filters_by_language(tmp_path):
    """Skills parse with or without front matter and filter by language and category."""
    _skill(
        tmp_path,
        "c-memory",
        "Check every strcpy.\n",
        (
            "name: c-memory\ndescription: Memory safety checklist\n"
            "languages: [c, cpp]\ncategories: [vulnerability]"
        ),
    )
    _skill(
        tmp_path,
        "general",
        "Applies everywhere.\n",
        "description: General review hygiene",
    )
    _skill(tmp_path, "bare", "No frontmatter at all.\n")
    _skill(tmp_path, "broken", "x", "description: [unclosed")
    lib = SkillLibrary([tmp_path])
    assert {s.name for s in lib.skills} == {"c-memory", "general", "bare"}
    assert (
        lib.get("c-memory").description == "Memory safety checklist"
        and lib.get("c-memory").body == "Check every strcpy.\n"
    )
    assert lib.get("bare").description == "" and lib.get("bare").body.startswith(
        "No frontmatter"
    )
    assert any("broken" in e for e in lib.errors)
    assert {s.name for s in lib.applicable({"python"})} == {"general", "bare"}
    assert {s.name for s in lib.applicable({"c"})} == {"c-memory", "general", "bare"}
    assert {s.name for s in lib.applicable({"c"}, category="bug")} == {
        "general",
        "bare",
    }
    assert {s.name for s in lib.applicable({"c"}, category="vulnerability")} == {
        "c-memory",
        "general",
        "bare",
    }


def test_first_path_wins_on_duplicate_names_and_missing_dirs_are_fine(tmp_path):
    """A duplicate name is reported and the first path wins; missing dirs are fine."""
    a, b = tmp_path / "a", tmp_path / "b"
    _skill(a, "dup", "from a\n", "description: A")
    _skill(b, "dup", "from b\n", "description: B")
    lib = SkillLibrary([a, b, tmp_path / "missing"])
    assert lib.get("dup").body == "from a\n" and len(lib.skills) == 1
    assert any("dup" in e for e in lib.errors)


def test_shipped_skills_folder_is_valid():
    """The skills shipped in the repo load without errors and each has real content."""
    lib = SkillLibrary([REPO_SKILLS])
    assert lib.errors == [] and {"c-memory-safety", "python-injection"} <= {
        s.name for s in lib.skills
    }
    for s in lib.skills:
        assert s.description and len(s.body) > 200, s.name


def test_agents_get_skill_tools_and_a_list_of_applicable_skills(
    tool_context, tmp_path, registry
):
    """Reviewers and validators get the skill tools and a list of applicable skills."""
    _skill(
        tmp_path / "sk",
        "c-memory",
        "Check every strcpy and every length argument.\n",
        "description: C memory checklist\nlanguages: [c]",
    )
    _skill(
        tmp_path / "sk",
        "rust-only",
        "irrelevant here\n",
        "description: Rust\nlanguages: [rust]",
    )
    tool_context.skills = SkillLibrary([tmp_path / "sk"])
    tools = {t.name: t for t in review_tools(tool_context)}
    assert "skill_list" in tools and "skill_read" in tools
    assert not tools["skill_read"].untrusted, (
        "skills are operator content, like prompts"
    )
    listed = tools["skill_list"].handler({})
    assert [s["name"] for s in listed] == ["c-memory"], (
        "the demo change touches C and Python, not Rust"
    )
    assert "strcpy" in tools["skill_read"].handler({"name": "c-memory"})["body"]
    assert (
        tools["skill_read"]
        .handler({"name": "rust-only"})["body"]
        .startswith("irrelevant")
    ), "readable by name"
    assert "error" in tools["skill_read"].handler({"name": "nope"})

    msg = build_user_message(tool_context, ReviewPlan(), SUBAGENTS[0])
    assert "c-memory" in msg and "skill_read" in msg and "rust-only" not in msg
    agent = ValidatorAgent(
        tool_context.settings, registry, NullLog(), lambda r, f=None: None
    )
    vmsg = agent._user_message(
        tool_context,
        Finding(title="t", location=Location(path="native/greet.c", line=1)),
    )
    assert "c-memory" in vmsg and "skill_read" in vmsg


def test_lead_agent_has_skill_tools(tmp_path):
    """The chat lead agent also gets ``skill_list`` and ``skill_read``."""
    from types import SimpleNamespace

    _skill(tmp_path / "sk", "general", "Be thorough.\n", "description: General")
    session = SimpleNamespace(
        repo=tmp_path,
        memory=None,
        last_report=None,
        last_run_dir=None,
        skills=SkillLibrary([tmp_path / "sk"]),
    )
    tools = {t.name: t for t in lead_tools(session, SafetyFilter())}
    assert {"skill_list", "skill_read"} <= set(tools)
    assert tools["skill_list"].handler({})[0]["name"] == "general"


def test_pipeline_logs_the_loaded_skills_and_cli_lists_them(
    demo_repo, tmp_path, capsys
):
    """A run logs the skills it loaded and the ``skills`` command lists them."""
    repo, base, head = demo_repo
    _skill(
        tmp_path / "sk",
        "team-rules",
        "Never log secrets.\n",
        "description: Team conventions",
    )
    settings = Settings.load(
        overrides={
            "llm": {"mode": "offline"},
            "output_dir": str(tmp_path / "out"),
            "scanners": {"enabled": ["builtin"]},
            "skills": {"paths": [str(tmp_path / "sk")]},
        }
    )
    _report, run_dir = run_review(settings, repo, base, head)
    ev = next(
        e
        for e in ActivityLog.read(run_dir / "activity.jsonl")
        if e["event"] == "skills"
    )
    assert ev["names"] == ["team-rules"] and ev["count"] == 1
    (tmp_path / "cfg.yaml").write_text(f"skills:\n  paths: ['{tmp_path / 'sk'}']\n")
    assert cli.main(["skills", "--config", str(tmp_path / "cfg.yaml")]) == 0
    out = capsys.readouterr().out
    assert "team-rules" in out and "Team conventions" in out


def test_component_folders_decide_who_sees_a_skill(tmp_path):
    """A skill's folder decides which agents see it; top-level skills are shared."""
    root = tmp_path / "sk"
    _skill(
        root / "shared_skills",
        "c-memory",
        "Check every strcpy and every length argument here.\n",
        "description: C memory\nlanguages: [c]\ncategories: [bug, vulnerability]",
    )
    _skill(
        root / "bugs",
        "edge-cases",
        "Try n = 0, 1 and len(x) on every slice.\n",
        "description: Edges",
    )
    _skill(root / "validator", "proofs", "Run base and head.\n", "description: Proofs")
    _skill(
        root / "planner", "plan-well", "Rank files by risk.\n", "description: Planning"
    )
    _skill(root / "lead", "talk", "Lead with the numbers.\n", "description: Talking")
    _skill(root, "legacy-flat", "Old layout, still shared.\n", "description: Flat")
    (root / "notes").mkdir()  # not a component folder and no SKILL.md: reported
    lib = SkillLibrary([root])
    comp = {s.name: s.component for s in lib.skills}
    assert comp == {
        "c-memory": "shared",
        "edge-cases": "bugs",
        "proofs": "validator",
        "plan-well": "planner",
        "talk": "lead",
        "legacy-flat": "shared",
    }
    assert any("notes" in e for e in lib.errors)

    def names(skills):
        return {s.name for s in skills}

    assert names(lib.applicable({"c"}, "bug", component="bugs")) == {
        "c-memory",
        "edge-cases",
        "legacy-flat",
    }
    assert names(lib.applicable({"c"}, "style", component="style")) == {"legacy-flat"}
    assert names(
        lib.applicable({"python"}, "vulnerability", component="validator")
    ) == {"proofs", "legacy-flat"}
    assert lib.get("edge-cases", component="vulnerabilities") is None, (
        "another agent's folder stays closed"
    )
    assert lib.get("edge-cases", component="bugs") is not None
    assert names(lib.visible("lead")) == {"c-memory", "talk", "legacy-flat"}, (
        "the lead sees shared ones whatever the language"
    )

    from scr_agents.ws5_knowledge.skills import inline_skills, skills_section

    text = inline_skills(lib, "planner")
    assert "Rank files by risk." in text and "strcpy" not in text, (
        "the planner gets its own skills in full"
    )
    assert "edge-cases" in skills_section(lib, {"c"}, "bug", component="bugs")
    assert "edge-cases" not in skills_section(lib, {"c"}, "bug", component="style")


def test_switching_a_skill_off_is_stored_and_hides_it(tmp_path):
    """Disabling a skill persists in ``config.yaml`` and hides it until re-enabled."""
    root = tmp_path / "sk"
    _skill(root / "bugs", "edge-cases", "Try n = 0.\n", "description: Edges")
    lib = SkillLibrary([root])
    lib.set_enabled("edge-cases", False)
    assert "edge-cases" in (root / "config.yaml").read_text()
    again = SkillLibrary([root])
    s = again.skills[0]
    assert s.enabled is False and again.get("edge-cases", component="bugs") is None
    assert (
        again.applicable(set(), component="bugs") == [] and again.summary("bugs") == []
    )
    again.set_enabled("edge-cases", True)
    assert SkillLibrary([root]).get("edge-cases", component="bugs") is not None
    with pytest.raises(KeyError):
        again.set_enabled("nope", True)


def test_shipped_skills_have_one_folder_per_component():
    """The shipped skills have a starter skill and README in every component folder."""
    from scr_agents.ws5_knowledge.skills import COMPONENTS

    lib = SkillLibrary([REPO_SKILLS])
    have = {s.component for s in lib.skills}
    assert have == {"shared", *COMPONENTS}, "a starter skill in every folder"
    for folder in (
        "shared_skills",
        "planner",
        "orchestrator",
        "bugs",
        "vulnerabilities",
        "style",
        "validator",
        "lead",
    ):
        assert (REPO_SKILLS / folder / "README.md").is_file(), folder


def test_web_chat_lead_agent_gets_its_skills_fresh(tmp_path):
    """A console chat's lead agent gets its skills, and a switch applies at once.

    Seen while writing the how-to: console chats gave the lead agent no skills at all.
    """
    from starlette.testclient import TestClient

    from scr_agents.web.server import create_app

    root = tmp_path / "sk"
    _skill(root / "lead", "talk", "Lead with the numbers.\n", "description: Talking")
    _skill(root / "bugs", "edges", "Try n = 0.\n", "description: Edges")
    settings = Settings.load(
        overrides={
            "llm": {"mode": "offline"},
            "output_dir": str(tmp_path / "out"),
            "skills": {"paths": [str(root)]},
            "web": {"token": "t", "allowed_hosts": ["testserver"]},
        }
    )
    with TestClient(
        create_app(settings, token="t"), headers={"Authorization": "Bearer t"}
    ) as c:
        cid = c.post("/api/chats/new", json={"demo": True}).json()["chat_id"]
        chat = c.app.state.chats.get(cid, restore=False)
        tools = {t.name: t for t in lead_tools(chat, SafetyFilter())}
        assert [s["name"] for s in tools["skill_list"].handler({})] == ["talk"], (
            "shared + lead, not bugs"
        )
        c.post("/api/settings/skills/talk", json={"enabled": False})
        assert tools["skill_list"].handler({}) == [], (
            "a switch in the console applies to a running chat"
        )

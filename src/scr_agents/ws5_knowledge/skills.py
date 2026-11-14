"""Skills: operator-authored review guidance the agents pull in on demand.

A skill is a directory with a `SKILL.md`: YAML front matter (name, description,
languages, categories) followed by Markdown guidance. A skills root (`skills.paths`, the
repository's `skills/` folder by default) holds one folder per audience:

    shared_skills/<name>/SKILL.md
        every agent (still filtered by languages / categories)
    planner/ bugs/ vulnerabilities/ style/ validator/ lead/
        only that component

A skill directly under the root (`skills/<name>/SKILL.md`, the old layout) counts as
shared. `<root>/config.yaml` lists the skills switched off (`disabled: [...]`); the
console edits it. Skills are never read from the repository under review, so they are
trusted the way the system prompts are. Agents see the names and descriptions of the
skills that apply to them and read the body with the `skill_read` tool; the planner,
which has no tools, gets its own skills inline.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SKILL_FILE = "SKILL.md"
STATE = "config.yaml"
SHARED = "shared"
# folder -> component; the order is how the console lists them
FOLDERS: dict[str, str] = {
    "shared_skills": SHARED,
    "planner": "planner",
    "orchestrator": "orchestrator",
    "bugs": "bugs",
    "vulnerabilities": "vulnerabilities",
    "style": "style",
    "validator": "validator",
    "lead": "lead",
}
COMPONENTS = [c for c in FOLDERS.values() if c != SHARED]
LABELS = {
    SHARED: "Shared (every agent)",
    "planner": "Planner",
    "bugs": "Bug reviewer",
    "vulnerabilities": "Security reviewer",
    "style": "Style reviewer",
    "validator": "Validator",
    "orchestrator": "OpenFlow orchestrator",
    "lead": "Lead agent (chat)",
}
STATE_HEADER = (
    "# Skills switched off (by name). Written by the console "
    "(Configure -> Skills); editing by hand\n"
    "# is fine. Everything not listed here is on.\n"
)


@dataclass
class Skill:
    """One skill: the front matter of its ``SKILL.md``, its body and where it lives.

    Empty ``languages`` or ``categories`` lists match everything. ``component``
    is ``"shared"`` for a skill every agent sees, otherwise the one component
    that gets it.
    """

    name: str
    description: str
    body: str
    path: Path
    languages: list[str] = field(default_factory=list)  # empty = every language
    categories: list[str] = field(
        default_factory=list
    )  # empty = every subagent / finding category
    component: str = SHARED  # shared, or the one component that gets it
    enabled: bool = True
    root: Path | None = None

    def applies(self, languages: set[str], category: str | None = None) -> bool:
        """Return whether the skill fits the code under review.

        Args:
            languages: Languages of the code under review; the skill applies when
                it lists none or shares at least one.
            category: Subagent or finding category; the skill applies when it
                lists none or includes this one. ``None`` skips the check.
        """
        if self.languages and not (set(self.languages) & languages):
            return False
        return not (category and self.categories and category not in self.categories)

    def visible_to(self, component: str | None) -> bool:
        """Return whether the skill is switched on and meant for ``component``.

        Shared skills are meant for every component; ``None`` stands for any
        component.
        """
        return self.enabled and (
            component is None or self.component in (SHARED, component)
        )

    def summary(self) -> dict[str, Any]:
        """Return the skill's metadata without its body, ready for JSON."""
        return {
            "name": self.name,
            "description": self.description,
            "languages": self.languages,
            "categories": self.categories,
            "path": str(self.path),
            "component": self.component,
            "enabled": self.enabled,
        }


def parse_skill(text: str, default_name: str) -> tuple[dict[str, Any], str]:
    """Split `SKILL.md` into (front matter, body).

    No front matter is fine; invalid YAML is not.
    """
    if not text.startswith("---\n"):
        return {"name": default_name}, text
    end = text.find("\n---", 4)
    if end < 0:
        raise ValueError("front matter is not closed with ---")
    meta = yaml.safe_load(text[4:end]) or {}
    if not isinstance(meta, dict):
        raise TypeError("front matter must be a mapping")
    body = text[end + 4 :]
    body = body.removeprefix("\n")
    meta.setdefault("name", default_name)
    return meta, body


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def _disabled(root: Path) -> set[str]:
    try:
        data = yaml.safe_load((root / STATE).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return set()
    return (
        {str(x) for x in data.get("disabled") or []}
        if isinstance(data, dict)
        else set()
    )


class SkillLibrary:
    """Every skill found under the skills roots, switched on or off."""

    def __init__(self, paths: Iterable[str | Path]):
        """Load the skills under each root in ``paths``.

        Roots that are not directories are skipped. A skill that cannot be
        loaded (no ``SKILL.md``, bad front matter, a duplicate name) is left out
        and described in ``errors`` instead of raising.

        Args:
            paths: Skills roots, as configured in ``skills.paths``.
        """
        self.paths = [Path(p) for p in paths]
        self.skills: list[Skill] = []  # every skill found, switched on or off
        self.errors: list[str] = []
        self._by_name: dict[str, Skill] = {}
        self._lock = threading.Lock()
        for d in self.paths:
            if d.is_dir():
                self._load_root(d)

    def _load_root(self, root: Path) -> None:
        off = _disabled(root)
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if entry.name in FOLDERS:
                for skill_dir in sorted(
                    p
                    for p in entry.iterdir()
                    if p.is_dir() and not p.name.startswith(".")
                ):
                    self._load_skill(skill_dir, FOLDERS[entry.name], root, off)
            elif (entry / SKILL_FILE).is_file():
                self._load_skill(entry, SHARED, root, off)  # the flat layout: shared
            else:
                self.errors.append(
                    f"{entry}: not a skills folder (use one of {', '.join(FOLDERS)})"
                )

    def _load_skill(self, d: Path, component: str, root: Path, off: set[str]) -> None:
        f = d / SKILL_FILE
        if not f.is_file():
            self.errors.append(f"{d}: no {SKILL_FILE}")
            return
        try:
            meta, body = parse_skill(
                f.read_text(encoding="utf-8", errors="replace"), d.name
            )
            skill = Skill(
                name=str(meta.get("name") or d.name).strip(),
                description=" ".join(str(meta.get("description") or "").split()),
                body=body,
                path=f,
                languages=_as_list(meta.get("languages")),
                categories=_as_list(meta.get("categories")),
                component=component,
                root=root,
            )
        except (ValueError, TypeError, yaml.YAMLError) as e:
            self.errors.append(f"{f}: {str(e).splitlines()[0]}")
            return
        if skill.name in self._by_name:
            self.errors.append(
                f"{f}: duplicate skill name {skill.name!r} ignored "
                f"(already loaded from {self._by_name[skill.name].path})"
            )
            return
        skill.enabled = skill.name not in off
        self._by_name[skill.name] = skill
        self.skills.append(skill)

    # -------------------------------------------------------------- what the agents see
    def get(self, name: str, component: str | None = None) -> Skill | None:
        """Return the skill called ``name`` if ``component`` may read it, else ``None``.

        A skill that is switched off, or belongs to another component, is
        treated as missing.
        """
        s = self._by_name.get(str(name).strip())
        return s if s is not None and s.visible_to(component) else None

    def applicable(
        self,
        languages: set[str],
        category: str | None = None,
        component: str | None = None,
    ) -> list[Skill]:
        """Return the skills ``component`` sees that fit the languages and category.

        Args:
            languages: Languages of the code under review.
            category: Subagent or finding category, or ``None`` for any.
            component: Component asking, or ``None`` for every switched-on skill.
        """
        return [
            s
            for s in self.skills
            if s.visible_to(component) and s.applies(languages, category)
        ]

    def visible(self, component: str | None = None) -> list[Skill]:
        """Switched on and meant for `component`, whatever the languages.

        The chat lead agent's list.
        """
        return [s for s in self.skills if s.visible_to(component)]

    def own(self, component: str) -> list[Skill]:
        """Switched on and in `component`'s own folder (not the shared ones)."""
        return [s for s in self.skills if s.enabled and s.component == component]

    def summary(self, component: str | None = None) -> list[dict[str, Any]]:
        """Return the metadata of the skills ``visible`` lists for ``component``."""
        return [s.summary() for s in self.visible(component)]

    # ----------------------------------------------------------------- console edits
    def set_enabled(self, name: str, enabled: bool) -> Skill:
        """Switch a skill on or off in its root's config.yaml.

        Takes effect for runs started afterwards.
        """
        with self._lock:
            skill = self._by_name.get(name)
            if skill is None or skill.root is None:
                raise KeyError(f"no skill named {name!r}")
            off = _disabled(skill.root)
            off = off - {name} if enabled else off | {name}
            tmp = skill.root / (STATE + ".tmp")
            tmp.write_text(
                STATE_HEADER + yaml.safe_dump({"disabled": sorted(off)}),
                encoding="utf-8",
            )
            tmp.replace(skill.root / STATE)
            skill.enabled = enabled
            return skill


def skills_section(
    library: SkillLibrary | None,
    languages: set[str],
    category: str | None = None,
    component: str | None = None,
) -> str:
    """The lines an agent's task message carries so it knows which skills to read."""
    if library is None:
        return ""
    skills = library.applicable(languages, category, component)
    if not skills:
        return ""
    return (
        "Skills (operator guidance for this kind of code; call skill_read with the "
        "name before reviewing the matching code):\n"
        + "\n".join(f"- {s.name}: {s.description}" for s in skills)
        + "\n"
    )


def inline_skills(
    library: SkillLibrary | None, component: str, max_chars: int = 6000
) -> str:
    """The full text of a component's own skills, for an agent without tools.

    That agent is the planner. Skills are added in order and the text stops before the
    first one that would take it past ``max_chars``.
    """
    if library is None:
        return ""
    parts, used = [], 0
    for s in library.own(component):
        text = f"## {s.name}: {s.description}\n{s.body.strip()}\n"
        if used + len(text) > max_chars:
            break
        parts.append(text)
        used += len(text)
    return (
        ("Operator guidance for this step (skills):\n" + "\n".join(parts) + "\n")
        if parts
        else ""
    )

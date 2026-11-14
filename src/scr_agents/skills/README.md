# Skills

Step-by-step guide with examples and troubleshooting: `docs/scr_agents/extending.md` (in the built docs: SCR Agents, Extending).

A skill is reusable review guidance the agents pull in on demand: a checklist for a language, a
team's conventions, how to plan a review, what counts as proof. Each skill is a folder with a
`SKILL.md`, placed in the folder of the agents it is for:

```
src/scr_agents/skills/
  config.yaml                      which skills are off (written by the console; editing by hand is fine)
  shared_skills/                   every agent; filtered by languages and categories
    c-memory-safety/SKILL.md
    python-injection/SKILL.md
  planner/                         the planner (gets the full text inline: it has no tools)
    review-planning/SKILL.md
  orchestrator/                    the OpenFlow orchestrator (review.orchestrator: openflow)
    round-orchestration/SKILL.md
  bugs/                            the bug reviewer
    boundaries-and-error-paths/SKILL.md
  vulnerabilities/                 the security reviewer
    trust-boundaries/SKILL.md
  style/                           the style reviewer
    actionable-style/SKILL.md
  validator/                       the validator (second model, sandbox)
    proof-discipline/SKILL.md
  lead/                            the chat lead agent
    explaining-results/SKILL.md
```

An agent sees `shared_skills/` plus its own folder, never another agent's folder. A skill placed
directly in `src/scr_agents/skills/<name>/` (the older layout) counts as shared.

## SKILL.md

YAML front matter, then the guidance in Markdown:

```markdown
---
name: c-memory-safety                 # optional; defaults to the folder name; must be unique
description: What to check in C/C++ for memory safety   # one line, shown to the agents
languages: [c, cpp]                   # optional; offered only when the change touches these
categories: [vulnerability, bug]      # optional; offered only to these reviewers (and the validator
                                      # for findings of these categories)
---

The guidance itself. Keep it concrete: what to look for, where it hides, how to confirm it.
```

Language names are the ones `scr-agents` detects from file extensions: `python`, `c`, `cpp`, `go`,
`rust`, `javascript`, `typescript`, `java`, `kotlin`, `ruby`, `php`, `csharp`, `swift`, `shell`.
Categories are `bug`, `vulnerability`, `style`, `naming`, `performance`.

## How the agents use them

| folder | who | how |
|---|---|---|
| `shared_skills/` | all of the below | as for each agent, filtered by `languages` / `categories` |
| `planner/` | planner | full text in its prompt (it has no tools), about 6,000 characters at most |
| `orchestrator/` | OpenFlow orchestrator | `skill_list` / `skill_read` |
| `bugs/`, `vulnerabilities/`, `style/` | that reviewer | names in the task message; `skill_list` / `skill_read` tools |
| `validator/` | validator | names in each finding's task message; `skill_list` / `skill_read` |
| `lead/` | chat lead agent | `skill_list` / `skill_read` |

## Switching skills on and off

In the console: **Configure** (top left) -> **Skills**. Each skill has a switch; the change is written
to `src/scr_agents/skills/config.yaml` (`disabled: [names]`) and applies to reviews started afterwards. To add or edit
a skill, create or edit its `SKILL.md` here, then press **Reload** in the console.

## Where they are loaded from

`skills.paths: [builtin]` in the config: `builtin` is this folder, inside the `scr_agents` package.
Add other directories with the same layout to the list (relative paths resolve against the working
directory). Skills are operator content, like the system prompts: they are
never read from the repository under review, and they are not wrapped as untrusted text. Do not
put secrets in them. `scr-agents skills` lists what is loaded, per folder, and reports files that
failed to parse.

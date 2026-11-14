# Shared skills

Domain knowledge every agent can use: language checklists, CWE classes, a team's threat model. Filtered per change by `languages` and per agent by `categories` in the front matter (a skill with `categories: [vulnerability]` goes to the security reviewer and to the validator for vulnerability findings).

Reviewers and the validator get the names in their task message and read the text with `skill_read`; the chat lead agent lists them with `skill_list`.

One folder per skill: `src/scr_agents/skills/shared_skills/<name>/SKILL.md` (format in `src/scr_agents/skills/README.md`). Switch skills on or off in the console (Configure -> Skills).

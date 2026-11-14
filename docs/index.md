# SCR Agents

Sovereign agentic secure code review: `scr_agents` is a self-hosted multi-agent code reviewer.
Models are served locally; no online model API is called.

```{image} images/console-review.png
:alt: The web console after a review of the demo repository
:width: 100%
```

Start with {doc}`scr_agents/tour` for what a review looks like, or {doc}`scr_agents/overview` for
how it is built.

## Project tooling

The repository grew from a [uv](https://docs.astral.sh/uv/)-based Python project template and keeps
its tooling:

- **uv** for dependency management with a committed lockfile (`uv.lock`) and pinned Python version (`.python-version`)
- **src layout** with a console entry point
- **ruff** for linting and formatting, with a curated ruleset including Google-style docstring enforcement
- **mypy** in strict mode
- **pytest** for testing (CI passes cleanly even before the first test exists)
- **MLflow** for tracing LLM experiments: a local SQLite store that needs no setup, `.env`-based configuration for pointing at a shared server, and a worked example under `examples/`
- **OpenAI SDK** as the LLM client, since a self-hosted GPUStack model server speaks the OpenAI-compatible API
- **Sphinx docs** written in MyST Markdown with the Furo theme and an API reference generated from the docstrings
- **pre-commit hooks** that run the exact tool versions from `uv.lock`, and block direct commits and pushes to `main`
- **CI** (GitHub Actions) with `lint`, `typecheck`, `test`, and `docs` jobs on every pull request and push to `main`

```{toctree}
:maxdepth: 2
:caption: Guides

getting-started
mlflow
useful-commands
workflow
```

```{toctree}
:maxdepth: 2
:caption: SCR Agents

scr_agents/tour
scr_agents/overview
scr_agents/handover
scr_agents/commands
scr_agents/extending
```

```{toctree}
:maxdepth: 1
:caption: Reference

autoapi/index
```

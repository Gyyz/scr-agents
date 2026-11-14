# Useful commands

```bash
uv run scr-agents --help           # the reviewer's commands
uv run pytest                      # run tests
uv run ruff check .                # lint
uv run ruff format .               # format
uv run mypy                        # type check (strict, over src/ and examples/)
uv run mlflow ui                   # browse traces in the local store
uv run pre-commit run --all-files  # run every hook against the whole repo

uv run --group docs sphinx-build -W --keep-going -b html docs docs/_build/html  # build the docs; warnings fail, as in CI
uv run --group docs sphinx-build -b linkcheck docs docs/_build/linkcheck        # check external links (manual, not run in CI)

uv add <package>                   # add a runtime dependency
uv add --group dev <package>       # add a dev-only dependency (linters, test tools)
uv add --group docs <package>      # add a docs-only dependency (Sphinx extensions, themes)
uv remove <package>                # remove a dependency
uv sync                            # sync .venv with pyproject.toml/uv.lock
```

All of these update `pyproject.toml` and `uv.lock` together - never edit `uv.lock` by hand, and commit both files when they change.

After building the docs, open `docs/_build/html/index.html` in a browser to review them.

The API reference is generated from the docstrings in `src/`. Console entry points declared under `[project.scripts]` in `pyproject.toml` are left out, since they are commands rather than library API.

## Project structure

```text
.
|-- .github/workflows/
|   `-- ci.yml                   # CI: lint, typecheck, test, docs
|-- .pre-commit-config.yaml      # git hooks (commit and push stages)
|-- .env.example                 # documented settings and API keys (copy to .env)
|-- docs/
|   |-- conf.py                  # Sphinx config (reads name and version from pyproject.toml)
|   |-- index.md                 # docs landing page and table of contents
|   |-- getting-started.md
|   |-- mlflow.md
|   |-- useful-commands.md
|   `-- workflow.md              # the API reference is generated into docs/autoapi/ at build time
|-- pyproject.toml               # project metadata + ruff/mypy config + dependency groups
|-- uv.lock                      # locked dependency versions
|-- .python-version              # Python version pinned for uv
|-- README.md                    # short pitch and quickstart; the full docs live in docs/
|-- examples/
|   |-- mlflow_example.py        # traced GPUStack chat completion, start here
|   `-- multi_agent_example.py   # planner -> workers -> reviewer pipeline, one span per agent
`-- src/
    `-- scr_agents/              # the reviewer (see SCR Agents: Overview)
```

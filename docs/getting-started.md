# Getting started

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) installed. uv downloads the pinned Python version automatically, so no separate Python install is needed.

## Install

Clone this repository. Then:

```bash
# Install all dependencies (creates .venv from uv.lock)
uv sync

# Activate the git hooks (once per clone)
uv run pre-commit install

# Optional: local settings and API keys (see the comments inside)
cp .env.example .env
```

With GPUStack credentials in `.env`, start the tracking UI with `uv run mlflow ui`, run `uv run examples/mlflow_example.py` in a second terminal, then open `http://localhost:5000` and look under the experiment's **Traces** tab. See [Experiment tracking with MLflow](mlflow.md) for the details.

## Changing the Python version

The project pins Python 3.12 via `.python-version`. To switch:

1. Pin the new version (this rewrites `.python-version`):

   ```bash
   uv python pin 3.13
   ```

2. Update `requires-python` in `pyproject.toml` to match (e.g. `>=3.13`). This is required when pinning below the current floor, and recommended when raising it so ruff and mypy can assume the newer syntax.
3. Run `uv sync` - uv downloads the interpreter automatically, rebuilds `.venv`, and re-resolves `uv.lock` if `requires-python` changed.
4. Commit `.python-version` together with any `pyproject.toml`/`uv.lock` changes. CI reads the same pin via `setup-uv`, so no workflow edits are needed.

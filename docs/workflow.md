# Workflow and guardrails

Direct work on `main` is blocked at three layers:

1. **pre-commit hooks** (local): refuse commits made on `main` and pushes targeting `main`. Fast feedback, but bypassable with `--no-verify`, so not the enforcement layer.
2. **CI** (server): every pull request runs `lint`, `typecheck`, `test`, and `docs` with the same locked tool versions as the hooks, so bypassing the hooks locally only moves the failure to CI. The `docs` job treats every Sphinx warning as an error.
3. **Branch ruleset** (server): `main` requires a pull request with passing status checks, and blocks force pushes and deletion.

The intended flow: branch (`feature/...`, `fix/...`, `refactor/...`), commit, push, open a PR, merge when CI is green.

```{note}
The branch ruleset is repository configuration, not code, so it does not carry over automatically to repositories created from this template. Recreate it under **Settings -> Rules -> Rulesets** in each new repository, and add `docs` to the required status checks alongside `lint`, `typecheck`, and `test`.
```

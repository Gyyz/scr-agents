---
name: actionable-style
description: Which style and naming findings are worth a developer's time, and which to leave to the formatter
categories: [style, naming]
---

# Actionable style findings

A style finding is worth reporting when it makes the code harder to read correctly or easier to
break later. Everything a formatter or linter already enforces is not worth a finding.

## Report

- **Misleading names**: a function called `validate_user` that also writes to the database; a
  boolean named `enabled` that means disabled; units missing from names (`timeout` in ms or s?).
- **Inconsistency with the surrounding code**: a new module that uses camelCase where the package
  uses snake_case, a second way of doing something the codebase already does one way (two HTTP
  clients, two config loaders).
- **Dead or duplicated code** introduced by the change: unreachable branches, copies of an existing
  helper with a small edit, commented-out code.
- **Error messages and logs** that will not help someone debugging: "error", "failed", no value.
- **Public API surprises**: a changed signature or default that callers will not notice.

## Do not report

Line length, quote style, import order, trailing whitespace, brace placement, or anything else the
project's formatter (black, ruff format, prettier, gofmt, clang-format) fixes on save. Do not ask for
comments on obvious code or for type hints in a file that has none.

## Reporting

Severity is `low` or `info` unless the naming is actively misleading about behaviour (then `medium`,
category `naming`). Suggest the concrete rename or change in `suggested_fix`.

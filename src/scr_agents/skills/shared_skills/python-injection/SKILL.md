---
name: python-injection
description: Injection and unsafe-deserialization checklist for Python changes (shell, SQL, eval, pickle, YAML, templates)
languages: [python]
categories: [vulnerability]
---

# Python injection and unsafe deserialization

Follow each value from where it enters (request parameters, CLI arguments, files, environment,
messages from another service) to the sink. Report the sink with the path the data takes.

## Sinks to look for

- **Shell.** `subprocess.*` with `shell=True`, `os.system`, `os.popen`, `commands.*`. A list argv
  without `shell=True` is safe from shell metacharacters but still passes arguments the attacker
  chose; check for argument injection (`--output=/etc/...`) when the binary accepts options.
- **SQL.** String formatting or f-strings inside `execute()`, `cursor.execute("..." % x)`,
  `text()` in SQLAlchemy with interpolation, ORM `.raw()` / `.extra()`. Parameterised queries and
  bound parameters are the fix; identifiers (table and column names) need an allowlist.
- **Code.** `eval`, `exec`, `compile`, `__import__`/`importlib.import_module` with input,
  `getattr(obj, user_string)` on objects with dangerous attributes.
- **Deserialization.** `pickle.loads`, `marshal.loads`, `shelve`, `yaml.load` without
  `SafeLoader`, `jsonpickle.decode`, `dill`. Any of these on data from outside the process is
  remote code execution (CWE-502), whatever the surrounding checks look like.
- **Templates and HTML.** `Template(user_string)`, `render_template_string`, `Markup(...)` and
  `|safe` on input, `mark_safe`.
- **Paths.** `open(os.path.join(base, user_path))` without checking the resolved path stays under
  `base`; `zipfile`/`tarfile` extraction of untrusted archives (use `filter="data"`).
- **XML and regexes.** `xml.etree`/`lxml` on untrusted XML with entities enabled, regexes with
  nested quantifiers on unbounded input (ReDoS).

## How to confirm

1. Trace the input with `search_code` and `code_graph_query` to the sink; quote both ends.
2. For shell and SQL, write a proof of concept with `write_file` that calls the function with a
   payload such as `"; echo pwned"` or `' OR 1=1 --` and run it with `run_command` on the head
   checkout. Compare with base when the task is to decide whether the change introduced it.
3. For pickle or `yaml.load`, a payload that defines `__reduce__` (or `!!python/object/apply`)
   that creates a marker file is enough to show execution.
4. Map to CWE-78 (OS command), CWE-89 (SQL), CWE-94/95 (code injection, eval), CWE-502
   (deserialization), CWE-22 (path traversal), CWE-611 (XXE), CWE-1333 (ReDoS).

## What is not a finding

Scanner hits on test files, on constants, or on values that only come from the operator's own
configuration are leads to drop, with a sentence saying why. Say so in the rationale rather than
reporting them at low confidence.

---
name: review-planning
description: How to choose focus files and write hypotheses the reviewers can confirm or rule out
---

# Planning a review

The plan steers three reviewers and decides where the validator spends its sandbox time. A good plan
is short, specific and testable.

## Focus files

Rank the changed files by how much damage a mistake there could do, not by how much changed:

1. **Entry points for outside input**: request handlers, CLI argument parsing, message consumers,
   file and archive readers, deserialisers, template rendering.
2. **Privilege and trust boundaries**: authentication, authorisation checks, session and token
   handling, anything that builds a shell command, SQL, a path or a URL from data.
3. **Shared code with many callers**: a helper the code graph says is called from many places.
   A small change there has a wide blast radius.
4. **Native code and manual memory**: C/C++ buffers, `unsafe` Rust, FFI boundaries.
5. Tests, docs, generated files and lock files come last unless the change is only that.

List at most eight files. A file that only renames or reformats is not a focus file.

## Hypotheses

Each hypothesis names a function, the input that matters and the failure you expect:

- Good: "`load_settings()` in `app/utils.py` now calls `pickle.loads` on the request body, so a
  client can run code on the server."
- Good: "`last_n()` changed `items[-n:]` to `items[-n - 1:]`; it returns one element too many."
- Weak: "There may be security problems in utils.py."

A hypothesis the diff already disproves is noise; leave it out. When a commit message claims a fix,
add one hypothesis that the fix is incomplete (another caller, another input shape, the error path).

## Notes for the reviewers

One paragraph: what the change is for, which scanner hits look real, and anything a reviewer could
miss without the history (a renamed function, a moved check, a new dependency).

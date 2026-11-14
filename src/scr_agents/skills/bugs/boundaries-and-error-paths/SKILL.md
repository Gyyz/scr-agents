---
name: boundaries-and-error-paths
description: Where logic bugs hide in a change - boundaries, empty and None values, error paths, resources, concurrency
categories: [bug]
---

# Boundaries and error paths

Most real bugs sit where the happy path ends. For each changed function, walk these cases against the
code, not against the name of the function.

## Boundaries

- **Off by one**: slices (`items[-n:]` vs `items[-n - 1:]`), `range(len(x) - 1)`, `<` vs `<=`,
  inclusive end dates, page numbers starting at 0 or 1. Try n = 0, 1 and len(x).
- **Empty input**: empty list, empty string, empty file, zero rows. Does it divide by the count,
  index `[0]`, or call `max()` on nothing?
- **Missing values**: `None` / `null` / `undefined`, a missing dict key, an optional field the caller
  forgot. Follow the value until it is used.
- **Size and type**: integer overflow in C, float comparison with `==`, strings that are bytes,
  time zones, locale-dependent parsing.

## Error paths

- An exception caught and ignored (`except: pass`, empty `catch`), or logged and then the function
  continues with half-built state.
- A function that returns an error value the caller never checks (`err` ignored in Go, `-1` in C).
- Cleanup that does not run on the error path: files, sockets, locks, temporary directories,
  transactions left open. Look for `finally`, `with`, `defer`, RAII.
- Retries that repeat a non-idempotent action (payment, email, insert).

## State and concurrency

- Shared mutable state touched from threads, async tasks or request handlers without a lock.
- Check-then-act races: `if not exists(path): create(path)`.
- Caches that are never invalidated when the data they mirror changes.

## Reporting

Report one finding per defect with the line, the input that triggers it and what goes wrong. If you
can show it with a two-line test or a value the caller really passes, say so in `reproduction`.
Do not report a style preference as a bug.

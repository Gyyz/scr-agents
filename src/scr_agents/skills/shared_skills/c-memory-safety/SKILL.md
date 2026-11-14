---
name: c-memory-safety
description: What to check in C and C++ changes for memory safety, and how to confirm it with a run
languages: [c, cpp]
categories: [vulnerability, bug]
---

# C / C++ memory safety

Work from the data that enters the function, not from the function names. For every buffer,
answer three questions: who allocates it, what bounds it, who frees it.

## Where the bugs hide

- **Unbounded copies and formats.** `strcpy`, `strcat`, `sprintf`, `gets`, `scanf("%s")`, and
  `strncpy`/`snprintf` with a size that is not the destination size. Check the size argument
  against the *destination*, not the source, and check that the terminator fits.
- **Length arithmetic.** `len - 1` when `len` can be 0; `a + b` compared after it may have wrapped;
  `size_t` subtractions; a `malloc(n * sizeof(T))` where `n` comes from input (integer overflow
  feeding the allocation, CWE-190 then CWE-787).
- **Off-by-one at the boundary.** Loops with `<=`, indexes equal to the length, the terminator
  written one past the end, `sizeof(ptr)` instead of `sizeof(buf)`.
- **Lifetimes.** A pointer kept after `free`, `realloc` (the old pointer is dead), a returned
  pointer into a local array, a string from `strtok` or `getenv` stored for later.
- **Error paths.** Early returns that skip `free` or close (leak), or that free and then fall
  through to use (double free, use after free).
- **Signedness.** `char` used as an array index, `int` lengths compared with `size_t`, negative
  values from `read()`/`recv()` used as a count.

## How to confirm instead of guessing

1. Read the callers (`code_graph_query`) to learn whether the input can be attacker-controlled
   and how long it can be. A fixed-size buffer fed by a constant is not a finding.
2. Build with warnings as evidence when a compiler is available: `cc -Wall -Wextra -fsanitize=address
   -o /dev/null x.c` reports many of these at compile time; `-fsanitize=address` at run time
   turns an overflow into a crash with a stack trace.
3. Write a proof of concept with `write_file` (an input longer than the buffer, a size that
   overflows) and run it with `run_command` on the head checkout, then on base to show the change
   introduced it.
4. Map the weakness: CWE-120/121/122 for overflows, CWE-416 use after free, CWE-415 double free,
   CWE-190 integer overflow, CWE-131 wrong buffer size. `kb_lookup` has CERT C rules (STR31-C,
   ARR30-C, MEM30-C, INT30-C) for the report.

## Severity

Critical when the buffer is on an untrusted path (network input, file parsers, command line of a
setuid tool). High for reachable overflows with constrained input. Medium when it needs an unusual
configuration. A leak alone is low unless it is per request.

---
name: proof-discipline
description: How to confirm or reject a finding with evidence - minimal proofs, base vs head, and what does not count
---

# Proof discipline

Your verdict is only as good as the evidence behind it. Prefer running code over reading it, and
prefer a comparison over a single run.

## Build the smallest proof

1. Read the code at the reported line and the callers that matter. Decide what input would show the
   problem and what output proves it.
2. Write a proof of concept that calls the function directly (import it, do not start a server
   unless you must). Print the evidence: the returned value, the file created, the command run.
3. Run it on **head**. Then run the same script on **base**. Different behaviour on base and head is
   the strongest evidence there is: it shows the change caused it.

## What counts

- A run that shows the bad behaviour (wrong value, crash, injected command executed, file written
  outside the allowed directory).
- A failing test on head that passes on base, when the test exercises the reported code.
- A traced code path with file:line for each step, when running is impossible (say why).

## What does not count

- `ModuleNotFoundError`, a missing script, a build that fails for an unrelated reason: that is a setup
  failure, not evidence either way. Fix the setup (paths, `PYTHONPATH`, build first) or say the
  finding is unverified.
- A proof that only shows the function exists or that the code contains a pattern.
- The reviewer's description repeated back.

## Verdicts

- `confirmed`: you showed it, or traced it end to end with no gap.
- `rejected`: you showed the input cannot reach the sink, the check exists elsewhere, or the
  behaviour is intended and documented.
- `unverified`: anything in between. It is an honest answer, not a failure.

When the project memory says people rejected this finding before, confirm it again only with new,
concrete evidence. Propose a fix as a unified diff when you are confident it is correct.

# A tour in pictures

What a review looks like from the outside, in the order you meet it. The screenshots come from the
demo repository in offline mode, so you can reproduce every one of them without a model server:

```bash
uv run scr-agents --mode offline serve     # open the printed URL
```

For how the parts work inside, read the {doc}`overview` and the {doc}`handover`.

## The pipeline

```{image} ../images/architecture.svg
:alt: The review pipeline, from the request to the reports
:width: 100%
```

A request becomes two read-only checkouts. The code graph, the static scanners and the test bench
describe the change. The planner briefs three reviewers that work in parallel, their findings are
merged and ranked, and the validator checks each one on a second model. Text from the repository,
from tools and from MCP servers reaches a prompt only through the safety filter.

## Start a review

```{image} ../images/console-welcome.png
:alt: The console's welcome page with a box for a git URL
:width: 100%
```

Paste a git URL, optionally followed by what to look for, or choose **Try the demo repository**.
The switch at the top picks who runs a round: the fixed flow (**Deterministic**) or an LLM that
calls the reviewers and the validator as tools (**OpenFlow**).

## Watch it and read the result

```{image} ../images/console-review.png
:alt: The console after a review, with the review card and the side panels
:width: 100%
```

The review card shows the stages, the round, the counts and the findings. The panels on the right
follow the run: the orchestrator's phases and subagents, the MCP servers and their calls, memory
and the activity feed. The lead agent answers below the card in plain language.

## Open a finding

```{image} ../images/console-finding.png
:alt: A finding opened in the drawer, with the validator's comment and the verdict buttons
:width: 100%
```

A finding shows what is wrong, the reviewer's reasoning, the validator's comment and the evidence
the validator collected, such as the test run. **How the validator checked it** lists its steps.
**accept** and **reject** record your verdict.

## The reports

Every finished review has an HTML report: one self-contained page you can open or send.

```{image} ../images/review-report.png
:alt: The Review Report with the key findings and the list of all findings
:width: 100%
```

The **Review Report** holds the findings with their code, evidence and suggested fix. The
**Validator Report** is the validator's comment on that report, finding by finding, with your
verdict and whether the finding is settled.

```{image} ../images/validator-report.png
:alt: The Validator Report with a verdict and comment for each finding
:width: 100%
```

## Review rounds

```{image} ../images/review-rounds.svg
:alt: Review rounds and when a finding is settled
:width: 100%
```

A finding is settled when you agree with the validator. When findings are rejected, unverified or
disputed, the console offers **Review again**; the next round starts from the planner with the
pending findings and both reports as context, and its reports replace those findings. Settled
findings are carried along and not reported twice. `review.max_rounds` (3 by default) limits the
rounds in total.

Without the console:

```bash
uv run scr-agents verdict --run <run-id> --finding <finding-id> --verdict accept
uv run scr-agents next-round --run <run-id>
uv run scr-agents review --repo . --base main --auto-rounds
```

With `--auto-rounds` nobody is asked: the validator's confirmed and rejected verdicts settle a
finding and only unverified ones are reviewed again.

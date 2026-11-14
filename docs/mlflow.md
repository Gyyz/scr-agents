# Experiment tracking with MLflow

MLflow reads `MLFLOW_TRACKING_URI` and `MLFLOW_EXPERIMENT_NAME` from the environment on its own, so a script needs two lines before its first LLM call: load `.env`, then enable autologging for the SDK in use.

```python
import mlflow
from dotenv import load_dotenv

load_dotenv()
mlflow.openai.autolog()
```

From then on every call through the OpenAI SDK is recorded as a trace. MLflow has the same one-liner for Anthropic, LiteLLM, LangChain, Gemini, Bedrock, and more; add the SDK with `uv add` and call its `autolog()`. `examples/mlflow_example.py` is the complete, runnable version.

The OpenAI SDK ships with the template because GPUStack exposes an OpenAI-compatible API. Point the client at it with the two variables from `.env.example`:

```python
import os

from openai import OpenAI

client = OpenAI(
    base_url=os.environ["GPUSTACK_BASE_URL"],
    api_key=os.environ["GPUSTACK_API_KEY"],
)
```

Every call through that client becomes an LLM span carrying the GPUStack model name.

## Quick tour

With GPUStack credentials in `.env`, start the tracking UI with `uv run mlflow ui`, run `uv run examples/mlflow_example.py` in a second terminal, then open `http://localhost:5000` and look under the experiment's **Traces** tab.

## Configuration

Settings are plain environment variables, using MLflow's own names so they also apply to the `mlflow` CLI. Load them with `load_dotenv()` from a `.env` file; `.env.example` documents every variable.

| Variable | Default | Purpose |
| --- | --- | --- |
| `MLFLOW_TRACKING_URI` | `sqlite:///mlflow.db` | Tracking store: a local SQLite file or a tracking server URL. `.env.example` sets it to `http://localhost:5000`, the server `uv run mlflow ui` starts |
| `MLFLOW_EXPERIMENT_NAME` | `Default` | Experiment that groups this project's traces; set it to your project name |
| `MLFLOW_TRACKING_USERNAME` / `MLFLOW_TRACKING_PASSWORD` or `MLFLOW_TRACKING_TOKEN` | unset | Credentials for a shared server that requires them |

The SQLite default is the same store `uv run mlflow ui` opens when given no flags, so local traces appear in the UI without extra configuration. `mlflow.db`, `mlruns/`, and `.env` are gitignored.

## Instrumenting your own code

Autologging covers SDK calls. For your own steps, use MLflow's tracing API in either of two forms:

```python
import mlflow
from mlflow.entities import SpanType


# Arguments and the return value become the span's inputs and outputs.
@mlflow.trace(span_type=SpanType.CHAIN)
def answer(question: str) -> str:
    return generate(retrieve(question))


# An explicit span for a step MLflow cannot see into on its own.
with mlflow.start_span(name="rerank", span_type=SpanType.RERANKER) as span:
    span.set_inputs({"candidates": candidates})
    span.set_outputs({"ranked": ranked})
```

Trace the operations that matter for debugging (entry points, LLM calls, retrieval, tool calls) and skip trivial data shuffling. A trace is one end-to-end request made of spans; a run is a separate MLflow object that groups metrics and artifacts, produced by evaluations rather than by tracing.

`examples/multi_agent_example.py` puts this together: a planner agent splits a question into sub-questions, a worker agent answers each one, and a reviewer agent merges the findings. Every agent is a function decorated with `@mlflow.trace(span_type=SpanType.AGENT)` under one `CHAIN` root, and autologging nests the chat-model span for each completion beneath the agent that made it. Run it with `uv run examples/multi_agent_example.py` and open the trace to see the tree.

## Sharing traces with the team

Traces stay on your machine by default. To send them to a shared server instead, set one variable in `.env`:

```bash
MLFLOW_TRACKING_URI=http://mlflow.example.com:5000
# plus MLFLOW_TRACKING_USERNAME/PASSWORD or MLFLOW_TRACKING_TOKEN if the server requires them
```

To host such a server on a shared machine, run MLflow with a database backend and an artifact directory every client can reach:

```bash
uv run mlflow server --host 0.0.0.0 --port 5000 \
  --backend-store-uri sqlite:///mlflow.db \
  --default-artifact-root ./mlartifacts --serve-artifacts
```

For anything beyond a small team, swap SQLite for PostgreSQL and the artifact directory for object storage. The [tracking server guide](https://mlflow.org/docs/latest/self-hosting/architecture/tracking-server) covers both.

## Next steps

Once a project has real traces, MLflow's GenAI features build on the same store:

- **Evaluation**: score traces or a dataset with built-in and custom LLM judges via `mlflow.genai.evaluate`, comparing prompt or model variants as runs. [Evaluation quickstart](https://mlflow.org/docs/latest/genai/eval-monitor/quickstart/)
- **Prompt registry**: version prompts with `mlflow.genai.register_prompt` and load them with `mlflow.genai.load_prompt` so each trace links to the prompt version that produced it. [Version tracking quickstart](https://mlflow.org/docs/latest/genai/version-tracking/quickstart/)

Both need an LLM provider and API key, which is why the template leaves them to each project.

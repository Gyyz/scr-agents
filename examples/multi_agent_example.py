"""Trace a planner-workers-reviewer agent pipeline served by GPUStack.

Run from the project root so the ``.env`` file is found:

    uv run examples/multi_agent_example.py

Three kinds of agent cooperate on one question. A planner splits it into
sub-questions, a worker answers each one, and a reviewer merges the findings
into a final answer. Each agent is a function decorated with ``mlflow.trace``,
so the trace shows the pipeline as a tree: one CHAIN span at the root, one
AGENT span per agent call, and under each of those the chat-model span that
``mlflow.openai.autolog()`` records for the actual chat completion.

Start the server the traces go to with ``uv run mlflow ui`` first, then open
the experiment's **Traces** tab to inspect the tree.
"""

import os

import mlflow
from dotenv import load_dotenv
from mlflow.entities import SpanType
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

MODEL = "gemma4-31b-it"
MAX_SUB_QUESTIONS = 3


def build_client() -> OpenAI:
    """Create an OpenAI client pointed at the GPUStack server.

    Returns:
        A client configured from ``GPUSTACK_BASE_URL`` and ``GPUSTACK_API_KEY``.
    """
    return OpenAI(
        base_url=os.getenv("GPUSTACK_BASE_URL"),
        api_key=os.getenv("GPUSTACK_API_KEY"),
    )


def chat(client: OpenAI, system: str, user: str) -> str:
    """Send one system/user exchange to the model and return its reply.

    This is the only place the model is called. It is deliberately not
    decorated: autologging already records every completion as an LLM span,
    nested under whichever agent span is active.

    Args:
        client: The client to send the request through.
        system: The system prompt that sets the agent's role.
        user: The user message carrying the agent's task.

    Returns:
        The model's reply with surrounding whitespace removed, or an empty
        string when the model returned no content.
    """
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    response = client.chat.completions.create(model=MODEL, messages=messages)
    return (response.choices[0].message.content or "").strip()


@mlflow.trace(span_type=SpanType.AGENT)
def plan(client: OpenAI, question: str) -> list[str]:
    """Break a question into independent sub-questions for the workers.

    Args:
        client: The client to send the request through.
        question: The user's original question.

    Returns:
        At most ``MAX_SUB_QUESTIONS`` sub-questions, one per worker. Falls back
        to the original question when the planner produces nothing usable.
    """
    reply = chat(
        client,
        system=(
            "You are a planning agent. Split the user's question into at most "
            f"{MAX_SUB_QUESTIONS} independent sub-questions that together cover "
            "it. Reply with one sub-question per line and nothing else."
        ),
        user=question,
    )
    sub_questions = [line.strip(" -*0123456789.)") for line in reply.splitlines()]
    sub_questions = [s for s in sub_questions if s][:MAX_SUB_QUESTIONS]
    return sub_questions or [question]


@mlflow.trace(span_type=SpanType.AGENT)
def research(client: OpenAI, sub_question: str) -> str:
    """Answer one sub-question as a worker agent.

    Args:
        client: The client to send the request through.
        sub_question: The sub-question assigned to this worker.

    Returns:
        A short, factual answer to the sub-question.
    """
    return chat(
        client,
        system=(
            "You are a research agent. Answer the question accurately in at "
            "most three sentences. State clearly if you are unsure."
        ),
        user=sub_question,
    )


@mlflow.trace(span_type=SpanType.AGENT)
def review(client: OpenAI, question: str, findings: dict[str, str]) -> str:
    """Merge the workers' findings into one answer and flag any gaps.

    Args:
        client: The client to send the request through.
        question: The user's original question.
        findings: Each sub-question mapped to the worker's answer for it.

    Returns:
        The final answer, ending with a one-line note on anything the
        findings failed to cover.
    """
    notes = "\n\n".join(f"Q: {q}\nA: {a}" for q, a in findings.items())
    return chat(
        client,
        system=(
            "You are a reviewing agent. Using only the findings provided, "
            "write a clear answer to the original question. Finish with one "
            "line starting with 'Gaps:' naming anything the findings do not "
            "cover, or 'Gaps: none'."
        ),
        user=f"Original question: {question}\n\nFindings:\n{notes}",
    )


@mlflow.trace(span_type=SpanType.CHAIN)
def answer(client: OpenAI, question: str) -> str:
    """Run the full planner -> workers -> reviewer pipeline for one question.

    Args:
        client: The client every agent sends its requests through.
        question: The user's question.

    Returns:
        The reviewer's final answer.
    """
    sub_questions = plan(client, question)

    span = mlflow.get_current_active_span()
    if span is not None:
        span.set_attributes({"worker_count": len(sub_questions)})

    findings = {q: research(client, q) for q in sub_questions}
    return review(client, question, findings)


if __name__ == "__main__":
    load_dotenv()

    mlflow.set_experiment("Multi Agent Experiment")
    # Every call made through the OpenAI SDK from here on becomes an LLM span.
    mlflow.openai.autolog()

    result = answer(
        build_client(),
        "Why do IoT devices make attractive targets for botnets, and what can "
        "a small organisation do about it?",
    )
    print(result)
    print(f"\nTrace ID: {mlflow.get_last_active_trace_id()}")

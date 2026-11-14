"""Trace a chat completion served by GPUStack through the OpenAI SDK.

Run from the project root so the ``.env`` file is found:

    uv run examples/mlflow_example.py

MLflow reads ``MLFLOW_TRACKING_URI`` and ``MLFLOW_EXPERIMENT_NAME`` from the
environment on its own, so loading ``.env`` is all the configuration needed.
Start the server the traces go to with ``uv run mlflow ui`` first.
"""

import os

import mlflow
from dotenv import load_dotenv
from openai import OpenAI

if __name__ == "__main__":
    load_dotenv()

    # Every call made through the OpenAI SDK from here on becomes a trace.
    mlflow.openai.autolog()

    # GPUStack exposes an OpenAI-compatible API.
    client = OpenAI(
        base_url=os.getenv("GPUSTACK_BASE_URL"),
        api_key=os.getenv("GPUSTACK_API_KEY"),
    )

    client.chat.completions.create(
        model="gemma4-31b-it",
        messages=[
            {"role": "system", "content": "You are a helpful weather assistant."},
            {"role": "user", "content": "What's the weather like in Singapore?"},
        ],
    )

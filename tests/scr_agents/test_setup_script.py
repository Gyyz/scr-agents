"""deploy/setup.py: the interactive one-shot deployment generator.

The tests exercise it non-interactively.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from scr_agents.config import Settings

SETUP = Path(__file__).resolve().parents[2] / "deploy" / "setup.py"


def _run(args, tmp_path, fake_gpus="6"):
    env = {**os.environ, "SCR_AGENTS_SETUP_FAKE_GPUS": fake_gpus}
    proc = subprocess.run(
        [
            sys.executable,
            str(SETUP),
            "--out",
            str(tmp_path / "gen"),
            "--dry-run",
            *args,
        ],
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return tmp_path / "gen", proc.stdout


def test_defaults_generate_a_two_model_layout(tmp_path):
    """On six GPUs the defaults put Gemma on two GPUs and Llama on four."""
    gen, out = _run(["--yes"], tmp_path)
    assert "detected 6 GPU(s)" in out
    cfg = Settings.load(gen / "scr_agents.yaml")
    assert cfg.llm.mode == "local" and cfg.llm.local.model == "google/gemma-4-31B-it"
    assert cfg.llm.local.validator_model == "meta-llama/Llama-3.3-70B-Instruct"
    answers = json.loads((gen / "answers.json").read_text())
    assert answers["reviewer.gpus"] == ["0", "1"] and answers["validator.gpus"] == [
        "2",
        "3",
        "4",
        "5",
    ]
    assert (
        answers["reviewer.tool_call_parser"] == "gemma4"
        and answers["validator.tool_call_parser"] == "llama3_json"
    )
    assert (gen / "download_models.sh").read_text().count(
        "huggingface-cli download"
    ) == 2
    assert "tool_chat_template_gemma4.jinja" in (gen / "download_models.sh").read_text()
    # no docker on this host -> bare metal script
    if (gen / "start_llm.sh").exists():
        script = (gen / "start_llm.sh").read_text()
        assert (
            "CUDA_VISIBLE_DEVICES=0,1" in script
            and "CUDA_VISIBLE_DEVICES=2,3,4,5" in script
        )
        assert "--tensor-parallel-size 4" in script and "--quantization fp8" in script


def test_user_answers_override_defaults(tmp_path):
    """An answers file sets the image, models, GPUs, ports and scanners."""
    answers = {
        "deploy": "docker",
        "engine": "vllm",
        "image": "vllm/vllm-openai:v0.19.1",
        "reviewer.model": "Qwen/Qwen3-32B",
        "reviewer.gpus": ["0", "1", "2"],
        "reviewer.quantization": "none",
        "validator.enabled": True,
        "validator.model": "meta-llama/Llama-3.1-70B-Instruct",
        "validator.gpus": ["3", "4", "5"],
        "validator.quantization": "fp8",
        "console_port": 9000,
        "parallel_validations": 4,
        "scanners": ["builtin", "ruff"],
        "strict_offline": True,
    }
    (tmp_path / "answers.json").write_text(json.dumps(answers))
    gen, _ = _run(["--answers", str(tmp_path / "answers.json")], tmp_path)
    compose = yaml.safe_load((gen / "docker-compose.yml").read_text())
    svc = compose["services"]
    assert set(svc) == {"llm-reviewer", "llm-validator", "scr-agents"}
    assert svc["llm-reviewer"]["image"] == "vllm/vllm-openai:v0.19.1"
    rev_cmd = svc["llm-reviewer"]["command"]
    assert (
        "Qwen/Qwen3-32B" in rev_cmd
        and rev_cmd[rev_cmd.index("--tensor-parallel-size") + 1] == "3"
    )
    assert (
        rev_cmd[rev_cmd.index("--tool-call-parser") + 1] == "hermes"
    )  # suggested from the model name
    assert rev_cmd[rev_cmd.index("--reasoning-parser") + 1] == "qwen3"
    assert svc["llm-reviewer"]["deploy"]["resources"]["reservations"]["devices"][0][
        "device_ids"
    ] == ["0", "1", "2"]
    val_cmd = svc["llm-validator"]["command"]
    assert (
        "--quantization" in val_cmd
        and val_cmd[val_cmd.index("--quantization") + 1] == "fp8"
    )
    assert svc["scr-agents"]["ports"] == ["9000:9000"] and svc["scr-agents"][
        "networks"
    ] == ["internal"]
    assert "edge" not in compose["networks"]
    cfg = Settings.load(gen / "scr_agents.yaml")
    assert cfg.llm.local.base_url == "http://llm-reviewer:8000/v1"
    assert cfg.llm.local.validator_base_url == "http://llm-validator:8000/v1"
    assert cfg.llm.parallel_validations == 4 and cfg.scanners.enabled == [
        "builtin",
        "ruff",
    ]
    assert cfg.output_dir == "/data/out"


def test_single_gpu_single_model(tmp_path):
    """With one GPU the generator leaves out the separate validator model."""
    gen, _ = _run(["--yes"], tmp_path, fake_gpus="1")
    cfg = Settings.load(gen / "scr_agents.yaml")
    assert (
        cfg.llm.local.validator_model == "" and cfg.llm.local.validator_base_url == ""
    )
    answers = json.loads((gen / "answers.json").read_text())
    assert answers["validator.enabled"] is False and answers["reviewer.gpus"] == ["0"]


def test_interactive_prompts_take_typed_values_and_defaults(tmp_path):
    """Typed answers are used and empty lines take the defaults, in question order."""
    # Answers typed at the prompts, in question order; empty lines accept the default.
    lines = [
        "baremetal",  # deployment style
        "vllm",  # engine
        "",  # reviewer model -> default gemma-4-31B-it
        "0,1,2",  # reviewer GPUs (typed)
        "",  # tensor parallel -> 3 (follows the GPU count)
        "fp8",  # quantization (typed)
        "",
        "",
        "",  # max len, max seqs, utilization -> defaults
        "",
        "",
        "",  # tool parser, reasoning parser, template -> suggested
        "n",  # no validator
        "8010",  # reviewer port (typed)
        "8777",  # console port (typed)
        "",
        "",  # models dir, data dir
        "2",  # parallel validations (typed)
        "builtin",  # scanners (typed)
    ]
    typed = "\n".join(lines) + "\n"
    env = {
        **os.environ,
        "SCR_AGENTS_SETUP_FAKE_GPUS": "6",
        "SCR_AGENTS_SETUP_FORCE_INTERACTIVE": "1",
    }
    proc = subprocess.run(
        [sys.executable, str(SETUP), "--out", str(tmp_path / "gen"), "--dry-run"],
        input=typed,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    answers = json.loads((tmp_path / "gen" / "answers.json").read_text())
    assert (
        answers["reviewer.gpus"] == ["0", "1", "2"]
        and answers["reviewer.tensor_parallel"] == 3
    )
    assert (
        answers["reviewer.quantization"] == "fp8"
        and answers["reviewer.model"] == "google/gemma-4-31B-it"
    )
    assert answers["validator.enabled"] is False and answers["console_port"] == 8777
    assert answers["scanners"] == ["builtin"] and answers["parallel_validations"] == 2
    cfg = Settings.load(tmp_path / "gen" / "scr_agents.yaml")
    assert (
        cfg.llm.local.base_url == "http://127.0.0.1:8010/v1"
        and cfg.llm.parallel_validations == 2
    )
    script = (tmp_path / "gen" / "start_llm.sh").read_text()
    assert (
        "CUDA_VISIBLE_DEVICES=0,1,2" in script
        and "--quantization fp8" in script
        and "--port 8010" in script
    )

#!/usr/bin/env python3
"""One-shot interactive deployment for scr_agents.

Asks a few questions (every one has a default), generates the deployment files, and
optionally downloads the models, starts the services and runs the end-to-end check.
Standard library only, so it runs before anything is installed.

    python3 deploy/setup.py              # interactive
    python3 deploy/setup.py --yes        # accept every default
    python3 deploy/setup.py --answers deploy/generated/answers.json  # replay answers
    python3 deploy/setup.py --dry-run    # generate files, download and start nothing

Generated into deploy/generated/: docker-compose.yml or start_llm.sh, scr_agents.yaml,
download_models.sh, .env, answers.json.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# tool-call / reasoning parser suggestions by model family (vLLM and SGLang names
# differ)
PARSERS: list[tuple[str, dict[str, str | None]]] = [
    (
        "gemma-4",
        {
            "vllm_tool": "gemma4",
            "vllm_reasoning": "gemma4",
            "template": "tool_chat_template_gemma4.jinja",
            "sglang_tool": "gemma",
            "sglang_reasoning": None,
        },
    ),
    (
        "gemma4",
        {
            "vllm_tool": "gemma4",
            "vllm_reasoning": "gemma4",
            "template": "tool_chat_template_gemma4.jinja",
            "sglang_tool": "gemma",
            "sglang_reasoning": None,
        },
    ),
    (
        "llama-3",
        {
            "vllm_tool": "llama3_json",
            "vllm_reasoning": None,
            "template": "tool_chat_template_llama3.1_json.jinja",
            "sglang_tool": "llama3",
            "sglang_reasoning": None,
        },
    ),
    (
        "llama3",
        {
            "vllm_tool": "llama3_json",
            "vllm_reasoning": None,
            "template": "tool_chat_template_llama3.1_json.jinja",
            "sglang_tool": "llama3",
            "sglang_reasoning": None,
        },
    ),
    (
        "qwen3-coder",
        {
            "vllm_tool": "qwen3_coder",
            "vllm_reasoning": None,
            "template": None,
            "sglang_tool": "qwen3_coder",
            "sglang_reasoning": None,
        },
    ),
    (
        "qwen3",
        {
            "vllm_tool": "hermes",
            "vllm_reasoning": "qwen3",
            "template": None,
            "sglang_tool": "qwen25",
            "sglang_reasoning": "qwen3",
        },
    ),
    (
        "qwen2.5",
        {
            "vllm_tool": "hermes",
            "vllm_reasoning": None,
            "template": None,
            "sglang_tool": "qwen25",
            "sglang_reasoning": None,
        },
    ),
    (
        "deepseek-r1",
        {
            "vllm_tool": "deepseek_v3",
            "vllm_reasoning": "deepseek_r1",
            "template": None,
            "sglang_tool": "deepseekv3",
            "sglang_reasoning": "deepseek-r1",
        },
    ),
    (
        "deepseek-v3",
        {
            "vllm_tool": "deepseek_v3",
            "vllm_reasoning": None,
            "template": None,
            "sglang_tool": "deepseekv3",
            "sglang_reasoning": None,
        },
    ),
    (
        "mistral",
        {
            "vllm_tool": "mistral",
            "vllm_reasoning": None,
            "template": "tool_chat_template_mistral.jinja",
            "sglang_tool": "mistral",
            "sglang_reasoning": None,
        },
    ),
]
DEFAULT_PARSER: dict[str, str | None] = {
    "vllm_tool": "hermes",
    "vllm_reasoning": None,
    "template": None,
    "sglang_tool": "qwen25",
    "sglang_reasoning": None,
}
TEMPLATE_RAW = "https://raw.githubusercontent.com/vllm-project/vllm/main/examples"


# ----------------------------------------------------------------------------- helpers
class Asker:
    """Ask the setup questions, or answer them from saved answers or the defaults.

    Every non-secret answer ends up in ``self.answers``, which ``main`` writes to
    ``answers.json`` so that a later run can replay it.
    """

    def __init__(self, answers: dict[str, Any], accept_defaults: bool):
        """Start with no answers recorded.

        Args:
            answers: Saved answers by key; a key found here is never asked.
            accept_defaults: Take the default for every key not in ``answers``.
        """
        self.saved = answers
        self.yes = accept_defaults
        self.answers: dict[str, Any] = {}

    def ask(
        self,
        key: str,
        prompt: str,
        default: Any,
        kind: str = "str",
        choices: list[str] | None = None,
        secret: bool = False,
    ) -> Any:
        """Return the answer for ``key``: the saved one, the default or the typed one.

        The user is asked only when neither a saved answer nor ``accept_defaults``
        decides it and stdin is a terminal (or ``SCR_AGENTS_SETUP_FORCE_INTERACTIVE``
        is set); empty input takes the default, and input that does not parse is
        asked again.

        Args:
            key: Name of the answer in ``answers.json``.
            prompt: Question shown to the user.
            default: Answer for empty input and non-interactive runs.
            kind: ``str``, ``int``, ``bool`` (y or n) or ``list`` (comma or
                semicolon separated); decides how typed input is parsed.
            choices: Allowed values for typed ``str`` input, shown in the prompt.
            secret: Read the input without echo, do not show the default and do
                not record the answer.

        Returns:
            The answer. Saved answers and defaults are returned as they are.
        """
        if key in self.saved:
            value = self.saved[key]
        elif self.yes or not (
            sys.stdin.isatty() or os.environ.get("SCR_AGENTS_SETUP_FORCE_INTERACTIVE")
        ):
            value = default
        else:
            shown = (
                ""
                if secret and default
                else (f" [{default}]" if default not in (None, "") else "")
            )
            if choices:
                shown = f" ({'/'.join(choices)})" + shown
            while True:
                raw = (
                    getpass.getpass(f"{prompt}{shown}: ")
                    if secret
                    else input(f"{prompt}{shown}: ")
                ).strip()
                if not raw:
                    value = default
                    break
                if kind == "int":
                    try:
                        value = int(raw)
                        break
                    except ValueError:
                        print("  please enter a number")
                        continue
                if kind == "bool":
                    if raw.lower() in ("y", "yes", "true", "1"):
                        value = True
                        break
                    if raw.lower() in ("n", "no", "false", "0"):
                        value = False
                        break
                    print("  please answer y or n")
                    continue
                if kind == "list":
                    value = [
                        x.strip() for x in raw.replace(";", ",").split(",") if x.strip()
                    ]
                    break
                if choices and raw not in choices:
                    print(f"  choose one of {', '.join(choices)}")
                    continue
                value = raw
                break
        if not secret:
            self.answers[key] = value
        return value


def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` and capture its output as text, without raising on failure."""
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def detect_gpus() -> list[dict[str, Any]]:
    """List the NVIDIA GPUs as dicts with ``index``, ``name`` and ``memory_gb``.

    Empty when ``nvidia-smi`` is missing. ``SCR_AGENTS_SETUP_FAKE_GPUS`` set to a
    number fakes that many 48 GB GPUs (the tests use it).
    """
    fake = os.environ.get("SCR_AGENTS_SETUP_FAKE_GPUS")
    if fake:
        return [
            {"index": i, "name": "NVIDIA RTX 6000 Ada Generation", "memory_gb": 48}
            for i in range(int(fake))
        ]
    if not shutil.which("nvidia-smi"):
        return []
    proc = run(
        [
            "nvidia-smi",
            "--query-gpu=index,name,memory.total",
            "--format=csv,noheader,nounits",
        ]
    )
    gpus = []
    for line in proc.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3 and parts[0].isdigit():
            gpus.append(
                {
                    "index": int(parts[0]),
                    "name": parts[1],
                    "memory_gb": round(float(parts[2]) / 1024),
                }
            )
    return gpus


def detect_env() -> dict[str, Any]:
    """Probe the host: GPUs, CPUs, docker and its NVIDIA runtime, vLLM, SGLang, uv.

    Also records whether a Hugging Face token is set in the environment.
    """
    env = {
        "gpus": detect_gpus(),
        "docker": shutil.which("docker") is not None,
        "nvidia_container": False,
        "vllm": shutil.which("vllm") is not None,
        "sglang": run([sys.executable, "-c", "import sglang"]).returncode == 0,
        "uv": shutil.which("uv") is not None,
        "hf_token": bool(
            os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
        ),
        "cpus": os.cpu_count() or 1,
    }
    if env["docker"]:
        info = run(["docker", "info"])
        env["nvidia_container"] = "nvidia" in info.stdout.lower()
    return env


def guess_params_b(model: str) -> float | None:
    """Read the parameter count in billions from a model id, e.g. 31 for ``31B``.

    Returns:
        The number in front of the ``B`` in the last path component, or ``None``
        if the name has none.
    """
    m = re.search(r"(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z])", model.split("/")[-1])
    return float(m.group(1)) if m else None


def memory_estimate(
    model: str, quant: str, tp: int, gpu_mem_gb: float, util: float
) -> str:
    """Estimate whether a model fits its GPUs, as one line to show the user.

    Weights take 2 bytes per parameter (1 for fp8 or int8, 0.5 for int4, awq or
    gptq), split over ``tp`` GPUs. What is left of ``gpu_mem_gb * util`` on all of
    them is the KV cache budget: ok from 16 GB, tight above 0, else DOES NOT FIT.

    Args:
        model: Model id; the size is guessed from its name.
        quant: Quantization, such as ``none``, ``fp8``, ``awq`` or ``gptq``.
        tp: Tensor parallel size.
        gpu_mem_gb: Memory of one GPU in GB.
        util: Fraction of GPU memory the engine may use.

    Returns:
        The estimate, or a note that the size is unknown when the name does not
        give the parameter count.
    """
    params = guess_params_b(model)
    if params is None:
        return "size unknown from the name; watch vLLM's KV cache report after start"
    bytes_per = (
        1
        if quant in ("fp8", "int8")
        else 0.5
        if quant in ("int4", "awq", "gptq")
        else 2
    )
    weights = params * bytes_per
    per_gpu = weights / tp
    usable = gpu_mem_gb * util
    kv = (usable - per_gpu) * tp
    verdict = "ok" if kv >= 16 else "tight" if kv > 0 else "DOES NOT FIT"
    return (
        f"~{weights:.0f} GB weights -> {per_gpu:.0f} GB per GPU at TP={tp}; "
        f"~{max(kv, 0):.0f} GB left for KV cache in total ({verdict})"
    )


def parser_for(model: str) -> dict[str, str | None]:
    """Suggest the engine parsers and chat template for ``model`` from ``PARSERS``.

    The first family name contained in the lowercased model id wins; other models
    get ``DEFAULT_PARSER``.
    """
    low = model.lower()
    for key, p in PARSERS:
        if key in low:
            return p
    return DEFAULT_PARSER


def http_ok(url: str, timeout: float = 3.0) -> bool:
    """Return whether ``url`` answers with status 200 within ``timeout`` seconds."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # local URL
            return r.status == 200
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def dump_yaml(data: Any, path: Path) -> None:
    """Write ``data`` to ``path`` as YAML, or as JSON (valid YAML) without PyYAML."""
    try:
        import yaml  # type: ignore

        path.write_text(yaml.safe_dump(data, sort_keys=False, width=120))
    except ImportError:  # JSON is valid YAML
        path.write_text(json.dumps(data, indent=2) + "\n")


# ---------------------------------------------------------------------------- questions
def ask_instance(
    a: Asker,
    role: str,
    env: dict[str, Any],
    default_model: str,
    default_gpus: list[int],
    default_quant: str,
    engine: str,
) -> dict[str, Any]:
    """Ask for one inference server: model, GPUs, parallelism, limits and parsers.

    Prints a memory estimate for the answers.

    Args:
        a: The asker.
        role: ``reviewer`` or ``validator``; prefixes the answer keys.
        env: Host facts from ``detect_env``.
        default_model: Default Hugging Face model id.
        default_gpus: Default GPU ids.
        default_quant: Default quantization.
        engine: ``vllm`` or ``sglang``; picks the parser defaults.

    Returns:
        The instance settings that ``engine_args`` reads.
    """
    print(f"\n--- {role} model ---")
    model = a.ask(f"{role}.model", f"{role} model (Hugging Face id)", default_model)
    gpus = a.ask(
        f"{role}.gpus",
        f"GPU ids for the {role} (comma separated)",
        ",".join(map(str, default_gpus)),
        "list",
    )
    gpus = (
        [int(g) for g in gpus]
        if isinstance(gpus, list)
        else [int(x) for x in str(gpus).split(",") if x.strip()]
    )
    a.answers[f"{role}.gpus"] = [
        str(g) for g in gpus
    ]  # saved answers always hold the normalised list
    tp = a.ask(f"{role}.tensor_parallel", "tensor parallel size", len(gpus), "int")
    quant = a.ask(
        f"{role}.quantization",
        "quantization (none | fp8 | awq | gptq)",
        default_quant,
        choices=["none", "fp8", "awq", "gptq"],
    )
    max_len = a.ask(f"{role}.max_model_len", "max model length (tokens)", 65536, "int")
    max_seqs = a.ask(f"{role}.max_num_seqs", "max concurrent sequences", 16, "int")
    util = float(
        a.ask(f"{role}.gpu_memory_utilization", "GPU memory utilization", 0.92)
    )
    p = parser_for(model)
    tool_parser = a.ask(
        f"{role}.tool_call_parser",
        f"{engine} tool-call parser",
        p["vllm_tool"] if engine == "vllm" else p["sglang_tool"],
    )
    reasoning = a.ask(
        f"{role}.reasoning_parser",
        f"{engine} reasoning parser (empty = none)",
        (p["vllm_reasoning"] if engine == "vllm" else p["sglang_reasoning"]) or "",
    )
    template = (
        a.ask(
            f"{role}.chat_template",
            "vLLM tool chat template file (empty = model default)",
            p["template"] or "",
        )
        if engine == "vllm"
        else ""
    )
    gpu_mem = env["gpus"][0]["memory_gb"] if env["gpus"] else 48
    print(f"  memory: {memory_estimate(model, quant, tp, gpu_mem, util)}")
    return {
        "model": model,
        "gpus": gpus,
        "tp": tp,
        "quant": quant,
        "max_model_len": max_len,
        "max_num_seqs": max_seqs,
        "util": util,
        "tool_parser": tool_parser,
        "reasoning_parser": reasoning or None,
        "template": template or None,
    }


def collect(a: Asker, env: dict[str, Any]) -> dict[str, Any]:
    """Show what was detected, ask every question and return the deployment config.

    By default the reviewer gets the first third of the GPUs (at least one) and a
    validator on a second model gets the rest.
    """
    n = len(env["gpus"])
    print("=== scr_agents deployment setup ===")
    if env["gpus"]:
        print(
            f"detected {n} GPU(s): {env['gpus'][0]['name']} x{n}, "
            f"{env['gpus'][0]['memory_gb']} GB each; "
            f"{env['cpus']} CPU threads"
        )
    else:
        print(
            "no NVIDIA GPU detected (nvidia-smi missing); "
            "you can still generate files for another host"
        )
    print(
        f"docker: {'yes' if env['docker'] else 'no'}  "
        f"nvidia container runtime: {'yes' if env['nvidia_container'] else 'no'}  "
        f"vllm cli: {'yes' if env['vllm'] else 'no'}  "
        f"sglang: {'yes' if env['sglang'] else 'no'}  "
        f"uv: {'yes' if env['uv'] else 'no'}"
    )
    print("press Enter to accept the default shown in brackets\n")

    deploy_default = (
        "docker" if env["docker"] and env["nvidia_container"] else "baremetal"
    )
    deploy = a.ask(
        "deploy", "deployment style", deploy_default, choices=["docker", "baremetal"]
    )
    engine = a.ask("engine", "inference engine", "vllm", choices=["vllm", "sglang"])
    image = (
        a.ask(
            "image",
            f"{engine} container image",
            "vllm/vllm-openai:latest" if engine == "vllm" else "lmsysorg/sglang:latest",
        )
        if deploy == "docker"
        else None
    )

    # GPU split: reviewer gets the first third (min 1), validator the rest
    if n >= 2:
        n_rev = max(1, n // 3)
        rev_gpus, val_gpus = list(range(n_rev)), list(range(n_rev, n))
    else:
        rev_gpus, val_gpus = [0], []
    reviewer = ask_instance(
        a, "reviewer", env, "google/gemma-4-31B-it", rev_gpus, "none", engine
    )
    second = a.ask(
        "validator.enabled",
        "serve a second, different model as the validator? (y/n)",
        bool(val_gpus),
        "bool",
    )
    validator = None
    if second:
        validator = ask_instance(
            a,
            "validator",
            env,
            "meta-llama/Llama-3.3-70B-Instruct",
            val_gpus or rev_gpus,
            "fp8",
            engine,
        )

    print("\n--- scr_agents ---")
    reviewer_port = a.ask("reviewer.port", "reviewer API port on the host", 8000, "int")
    validator_port = (
        a.ask("validator.port", "validator API port on the host", 8001, "int")
        if second
        else None
    )
    console_port = a.ask("console_port", "web console port", 8765, "int")
    models_dir = a.ask("models_dir", "model cache directory", str(HERE / "models"))
    data_dir = a.ask(
        "data_dir", "data directory (reports, cloned projects)", str(HERE / "data")
    )
    validations = a.ask(
        "parallel_validations", "parallel validations", 6 if second else 3, "int"
    )
    scanners = a.ask(
        "scanners", "scanners to enable", "builtin,ruff,bandit,semgrep,cppcheck", "list"
    )
    if isinstance(scanners, str):
        scanners = [s.strip() for s in scanners.split(",") if s.strip()]
    a.answers["scanners"] = scanners
    strict_offline = (
        a.ask(
            "strict_offline",
            "strictly offline (no edge network; console via SSH tunnel)? (y/n)",
            False,
            "bool",
        )
        if deploy == "docker"
        else False
    )
    return {
        "deploy": deploy,
        "engine": engine,
        "image": image,
        "reviewer": reviewer,
        "validator": validator,
        "reviewer_port": reviewer_port,
        "validator_port": validator_port,
        "console_port": console_port,
        "models_dir": models_dir,
        "data_dir": data_dir,
        "parallel_validations": validations,
        "scanners": scanners,
        "strict_offline": strict_offline,
    }


# --------------------------------------------------------------------------- generation
def engine_args(
    inst: dict[str, Any], engine: str, port: int, template_dir: str
) -> list[str]:
    """Build the vLLM or SGLang server arguments for one instance.

    Args:
        inst: Instance settings from ``ask_instance``.
        engine: ``vllm`` or ``sglang``.
        port: Port the server listens on.
        template_dir: Directory of the chat templates, as the server sees it.

    Returns:
        The arguments that follow the ``vllm serve`` or ``sglang.launch_server``
        entry point.
    """
    if engine == "vllm":
        args = [
            "--model",
            inst["model"],
            "--served-model-name",
            inst["model"],
            "--tensor-parallel-size",
            str(inst["tp"]),
            "--max-model-len",
            str(inst["max_model_len"]),
            "--max-num-seqs",
            str(inst["max_num_seqs"]),
            "--gpu-memory-utilization",
            str(inst["util"]),
            "--enable-auto-tool-choice",
            "--tool-call-parser",
            inst["tool_parser"],
            # usage.prompt_tokens_details.cached_tokens: prefix (KV) cache hits per
            # call, shown in the console
            "--enable-prompt-tokens-details",
            "--port",
            str(port),
        ]
        if inst["quant"] != "none":
            args += ["--quantization", inst["quant"]]
        if inst["reasoning_parser"]:
            args += ["--reasoning-parser", inst["reasoning_parser"]]
        if inst["template"]:
            args += ["--chat-template", f"{template_dir}/{inst['template']}"]
        return args
    args = [
        "--model-path",
        inst["model"],
        "--tp",
        str(inst["tp"]),
        "--context-length",
        str(inst["max_model_len"]),
        "--mem-fraction-static",
        str(inst["util"]),
        "--tool-call-parser",
        inst["tool_parser"],
        # cached prompt tokens in usage, like vLLM's --enable-prompt-tokens-details
        "--enable-cache-report",
        "--port",
        str(port),
        "--host",
        "0.0.0.0",
    ]
    if inst["quant"] != "none":
        args += ["--quantization", inst["quant"]]
    if inst["reasoning_parser"]:
        args += ["--reasoning-parser", inst["reasoning_parser"]]
    return args


def compose_document(cfg: dict[str, Any]) -> dict[str, Any]:
    """Build the docker compose document: one service per model, plus the console.

    The model servers are only on an internal network. The console joins it, and
    also an ``edge`` network unless ``strict_offline`` is set, and starts once the
    model services are healthy.
    """
    engine = cfg["engine"]
    entry = (
        ["vllm", "serve"]
        if engine == "vllm"
        else ["python3", "-m", "sglang.launch_server"]
    )
    services: dict[str, Any] = {}
    for role, inst in (("reviewer", cfg["reviewer"]), ("validator", cfg["validator"])):
        if inst is None:
            continue
        cmd = engine_args(inst, engine, 8000, "/templates")
        services[f"llm-{role}"] = {
            "image": cfg["image"],
            "restart": "unless-stopped",
            "ipc": "host",
            "entrypoint": entry,
            "command": cmd,
            "environment": {"HF_HOME": "/models", "HF_HUB_OFFLINE": "1"},
            "volumes": [
                f"{cfg['models_dir']}:/models",
                f"{HERE / 'generated' / 'templates'}:/templates:ro",
            ],
            "networks": ["internal"],
            "healthcheck": {
                "test": ["CMD", "curl", "-sf", "http://localhost:8000/v1/models"],
                "interval": "30s",
                "timeout": "5s",
                "retries": 60,
                "start_period": "900s",
            },
            "deploy": {
                "resources": {
                    "reservations": {
                        "devices": [
                            {
                                "driver": "nvidia",
                                "device_ids": [str(g) for g in inst["gpus"]],
                                "capabilities": ["gpu"],
                            }
                        ]
                    }
                }
            },
        }
    networks: dict[str, Any] = {"internal": {"internal": True}}
    cc_networks = ["internal"]
    if not cfg["strict_offline"]:
        networks["edge"] = {}
        cc_networks.append("edge")
    services["scr-agents"] = {
        "build": {"context": str(ROOT), "dockerfile": "deploy/Dockerfile"},
        "restart": "unless-stopped",
        "depends_on": {name: {"condition": "service_healthy"} for name in services},
        "command": [
            "serve",
            "--host",
            "0.0.0.0",
            "--port",
            str(cfg["console_port"]),
            "--config",
            "/app/deploy/generated/scr_agents.yaml",
        ],
        "volumes": [
            f"{cfg['data_dir']}:/data",
            (
                f"{HERE / 'generated' / 'scr_agents.yaml'}"
                ":/app/deploy/generated/scr_agents.yaml:ro"
            ),
        ],
        "ports": [f"{cfg['console_port']}:{cfg['console_port']}"],
        "networks": cc_networks,
        "cpus": str(max(8, (os.cpu_count() or 16) // 3)),
        "mem_limit": "128g",
        "pids_limit": 4096,
        "security_opt": ["no-new-privileges:true"],
        "cap_drop": ["ALL"],
        "environment": {
            "SCR_AGENTS_MODE": "local",
            "SCR_AGENTS_TOKEN": "${SCR_AGENTS_TOKEN:-}",
        },
    }
    return {"services": services, "networks": networks}


def scr_agents_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Build the ``scr_agents.yaml`` settings that point at the served models.

    ``max_diff_chars`` is derived from the smaller ``max_model_len`` of the two.
    """
    docker = cfg["deploy"] == "docker"
    rev_url = (
        "http://llm-reviewer:8000/v1"
        if docker
        else f"http://127.0.0.1:{cfg['reviewer_port']}/v1"
    )
    val = cfg["validator"]
    val_url = (
        (
            "http://llm-validator:8000/v1"
            if docker
            else f"http://127.0.0.1:{cfg['validator_port']}/v1"
        )
        if val
        else ""
    )
    max_len = min(
        cfg["reviewer"]["max_model_len"],
        val["max_model_len"] if val else cfg["reviewer"]["max_model_len"],
    )
    return {
        "llm": {
            "mode": "local",
            "local": {
                "base_url": rev_url,
                "model": cfg["reviewer"]["model"],
                "validator_base_url": val_url,
                "validator_model": val["model"] if val else "",
                "temperature": 0.2,
                "max_tokens": 8000,
                "timeout_s": 900,
                "json_repair": True,
                "extra_body": {},
            },
            "parallel_subagents": 3,
            "parallel_validations": cfg["parallel_validations"],
            "max_findings_to_validate": 40,
            "max_turns": 30,
        },
        "scanners": {
            "enabled": cfg["scanners"],
            "only_changed_lines": True,
            "timeout_s": 600,
            "semgrep_config": "/opt/semgrep-rules" if docker else "auto",
        },
        "shell": {"timeout_s": 600, "max_output_chars": 30000},
        "bench": {"runner": "local", "timeout_s": 1800},
        "safety": {"max_chars": 80000},
        "output_dir": "/data/out" if docker else str(Path(cfg["data_dir"]) / "out"),
        "projects_dir": "/data/projects"
        if docker
        else str(Path(cfg["data_dir"]) / "projects"),
        "max_diff_chars": max(20000, min(60000, max_len - 8000)),
        "whole_repo_max_files": 800,
    }


def baremetal_script(cfg: dict[str, Any]) -> str:
    """Render ``start_llm.sh``, which starts each model server on its GPUs.

    The servers run in the background with logs in ``logs/``; ``start_llm.sh stop``
    kills them.
    """
    engine = cfg["engine"]
    entry = "vllm serve" if engine == "vllm" else "python3 -m sglang.launch_server"
    lines = [
        "#!/usr/bin/env bash",
        "# generated by deploy/setup.py",
        "set -euo pipefail",
        'cd "$(dirname "$0")"',
        f'export HF_HOME="{cfg["models_dir"]}" HF_HUB_OFFLINE=1',
        "mkdir -p logs",
        'if [[ "${1:-}" == "stop" ]]; then pkill -f "'
        + entry.split()[0]
        + '" || true; exit 0; fi',
        "",
    ]
    for role, inst, port in (
        ("reviewer", cfg["reviewer"], cfg["reviewer_port"]),
        ("validator", cfg["validator"], cfg["validator_port"]),
    ):
        if inst is None:
            continue
        args = " ".join(engine_args(inst, engine, port, "templates"))
        gpus = ",".join(str(g) for g in inst["gpus"])
        lines.append(
            f"CUDA_VISIBLE_DEVICES={gpus} nohup {entry} {args} > logs/{role}.log 2>&1 &"
        )
        lines.append(f'echo "{role}  -> http://127.0.0.1:{port}/v1  (logs/{role}.log)"')
    lines.append(
        'echo "when the services answer /v1/models: '
        'uv run scr-agents --config deploy/generated/scr_agents.yaml llm check"'
    )
    return "\n".join(lines) + "\n"


def download_script(cfg: dict[str, Any]) -> str:
    """Render ``download_models.sh``, the one online step: models and templates."""
    models = [cfg["reviewer"]["model"]] + (
        [cfg["validator"]["model"]] if cfg["validator"] else []
    )
    templates = {
        i["template"]
        for i in (cfg["reviewer"], cfg["validator"])
        if i and i.get("template")
    }
    lines = [
        "#!/usr/bin/env bash",
        "# generated by deploy/setup.py: one-time online step",
        "set -euo pipefail",
        'cd "$(dirname "$0")"',
        f'export HF_HOME="{cfg["models_dir"]}"',
        'mkdir -p "$HF_HOME" templates',
        'python3 -m pip install --quiet --upgrade "huggingface_hub[cli]"',
    ]
    lines += [f"huggingface-cli download {m}" for m in models]
    lines += [
        f'curl -fsSL "{TEMPLATE_RAW}/{t}" -o "templates/{t}"' for t in sorted(templates)
    ]
    lines.append('echo "done: models in $HF_HOME"')
    return "\n".join(lines) + "\n"


def write_outputs(
    cfg: dict[str, Any], out: Path, answers: dict[str, Any]
) -> list[Path]:
    """Write the generated files into ``out`` and return their paths.

    The files are ``scr_agents.yaml``, ``docker-compose.yml`` or ``start_llm.sh``,
    ``download_models.sh``, ``.env`` and ``answers.json`` (holding ``answers``).
    """
    out.mkdir(parents=True, exist_ok=True)
    (out / "templates").mkdir(exist_ok=True)
    written = []
    dump_yaml(scr_agents_config(cfg), out / "scr_agents.yaml")
    written.append(out / "scr_agents.yaml")
    if cfg["deploy"] == "docker":
        dump_yaml(compose_document(cfg), out / "docker-compose.yml")
        written.append(out / "docker-compose.yml")
    else:
        p = out / "start_llm.sh"
        p.write_text(baremetal_script(cfg))
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        written.append(p)
    d = out / "download_models.sh"
    d.write_text(download_script(cfg))
    d.chmod(d.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    written.append(d)
    (out / ".env").write_text(
        f"HF_HOME={cfg['models_dir']}\nHF_HUB_OFFLINE=1\nSCR_AGENTS_MODE=local\n"
    )
    (out / "answers.json").write_text(json.dumps(answers, indent=2) + "\n")
    written += [out / ".env", out / "answers.json"]
    return written


# ----------------------------------------------------------------------------- actions
def do_download(cfg: dict[str, Any], out: Path, a: Asker) -> None:
    """Run ``download_models.sh`` with a Hugging Face token, or skip it without one.

    The token comes from ``HF_TOKEN`` or ``HUGGING_FACE_HUB_TOKEN``, else it is
    asked for as a secret (not echoed, not saved).
    """
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if not token:
        token = a.ask(
            "hf_token",
            (
                "Hugging Face token for the gated models "
                "(leave empty to skip the download)"
            ),
            "",
            secret=True,
        )
    if not token:
        print(
            "no token: skipping the download; "
            "run deploy/generated/download_models.sh later with HF_TOKEN set"
        )
        return
    env = {**os.environ, "HF_TOKEN": token, "HUGGING_FACE_HUB_TOKEN": token}
    print("downloading models (this is the only online step)...")
    subprocess.run(["bash", str(out / "download_models.sh")], env=env, check=True)


def do_start(cfg: dict[str, Any], out: Path) -> list[str]:
    """Start the services with ``docker compose up`` or ``start_llm.sh``.

    Returns:
        The URLs that answer once the services are up: the console's
        ``/api/config`` for docker, else each model server's ``/v1/models``.
    """
    if cfg["deploy"] == "docker":
        print("starting services with docker compose...")
        subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(out / "docker-compose.yml"),
                "up",
                "-d",
                "--build",
            ],
            check=True,
        )
        return [f"http://127.0.0.1:{cfg['console_port']}/api/config"]
    print("starting the inference services...")
    subprocess.run(["bash", str(out / "start_llm.sh")], check=True)
    urls = [f"http://127.0.0.1:{cfg['reviewer_port']}/v1/models"]
    if cfg["validator"]:
        urls.append(f"http://127.0.0.1:{cfg['validator_port']}/v1/models")
    return urls


def wait_healthy(urls: list[str], timeout_s: int = 1800) -> bool:
    """Poll ``urls`` every 15 seconds until all answer or ``timeout_s`` runs out.

    Returns:
        ``True`` if every URL answered in time.
    """
    deadline = time.time() + timeout_s
    pending = list(urls)
    while pending and time.time() < deadline:
        pending = [u for u in pending if not http_ok(u)]
        if pending:
            print(
                f"  waiting for {', '.join(pending)} ... "
                f"({int(deadline - time.time())}s left)"
            )
            time.sleep(15)
    return not pending


def do_check(cfg: dict[str, Any], out: Path) -> None:
    """Run ``scr-agents llm check`` against the new services; a failure is not fatal.

    For docker it runs inside the console container, otherwise through ``uv run``
    when uv is installed, else with this Python.
    """
    if cfg["deploy"] == "docker":
        cmd = [
            "docker",
            "compose",
            "-f",
            str(out / "docker-compose.yml"),
            "exec",
            "-T",
            "scr-agents",
            "scr-agents",
            "llm",
            "check",
        ]
    elif shutil.which("uv"):
        cmd = [
            "uv",
            "run",
            "scr-agents",
            "--config",
            str(out / "scr_agents.yaml"),
            "llm",
            "check",
        ]
    else:
        cmd = [
            sys.executable,
            "-m",
            "scr_agents.cli",
            "--config",
            str(out / "scr_agents.yaml"),
            "llm",
            "check",
        ]
    print("running `scr-agents llm check`...")
    subprocess.run(cmd, cwd=str(ROOT), check=False)


def main(argv: list[str] | None = None) -> int:
    """Run the setup: ask, write the files, then download, start and check.

    Args:
        argv: Arguments without the program name; ``None`` reads ``sys.argv``.

    Returns:
        0, or 1 if the started services did not become healthy in time.
    """
    ap = argparse.ArgumentParser(
        description="interactive one-shot deployment for scr_agents"
    )
    ap.add_argument(
        "--yes", "-y", action="store_true", help="accept every default without asking"
    )
    ap.add_argument(
        "--answers",
        help="JSON file with saved answers (keys as in generated/answers.json)",
    )
    ap.add_argument(
        "--out",
        default=str(HERE / "generated"),
        help="where to write the generated files",
    )
    ap.add_argument("--dry-run", action="store_true", help="generate files only")
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--no-start", action="store_true")
    ap.add_argument("--no-check", action="store_true")
    args = ap.parse_args(argv)

    saved = json.loads(Path(args.answers).read_text()) if args.answers else {}
    a = Asker(saved, args.yes)
    env = detect_env()
    cfg = collect(a, env)
    out = Path(args.out).resolve()
    written = write_outputs(cfg, out, a.answers)
    print("\ngenerated:")
    for p in written:
        print(f"  {p}")
    if args.dry_run:
        return 0

    interactive = not args.yes and sys.stdin.isatty() and not saved
    if not args.no_download and (
        not interactive
        or a.ask("do_download", "download the models now? (y/n)", True, "bool")
    ):
        do_download(cfg, out, a)
    if not args.no_start and (
        not interactive
        or a.ask("do_start", "start the services now? (y/n)", True, "bool")
    ):
        urls = do_start(cfg, out)
        if wait_healthy(urls):
            print("services are up")
            if not args.no_check:
                do_check(cfg, out)
        else:
            print("services did not become healthy in time; check the logs")
            return 1
    print(
        "\nnext: "
        + (
            (
                f"open http://<server>:{cfg['console_port']}  or  "
                f"docker compose -f {out / 'docker-compose.yml'} "
                f"exec -it scr-agents scr-agents chat <repo-url>"
            )
            if cfg["deploy"] == "docker"
            else (
                f"uv run scr-agents --config {out / 'scr_agents.yaml'} "
                "chat <repo-path-or-url>"
            )
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env bash
# Bare-metal alternative to docker compose: two vLLM instances on the 6 GPUs.
#   GPUs 0-1  reviewer   google/gemma-4-31B-it             bf16, TP=2  -> :8000
#   GPUs 2-5  validator  meta-llama/Llama-3.3-70B-Instruct fp8,  TP=4  -> :8001
# Run deploy/download_models.sh first. Requires vLLM >= 0.19 for the gemma4 parsers.
#   deploy/start_vllm.sh            # starts both in the background, logs in deploy/logs/
#   deploy/start_vllm.sh stop
set -euo pipefail
cd "$(dirname "$0")"
export HF_HOME="$PWD/models" HF_HUB_OFFLINE=1
mkdir -p logs

if [[ "${1:-}" == "stop" ]]; then
  pkill -f "vllm serve" || true
  exit 0
fi

CUDA_VISIBLE_DEVICES=0,1 nohup vllm serve google/gemma-4-31B-it \
    --served-model-name google/gemma-4-31B-it \
    --tensor-parallel-size 2 --max-model-len 65536 --max-num-seqs 16 --gpu-memory-utilization 0.92 \
    --enable-auto-tool-choice --tool-call-parser gemma4 --reasoning-parser gemma4 \
    --enable-prompt-tokens-details \
    --chat-template templates/tool_chat_template_gemma4.jinja \
    --port 8000 > logs/reviewer.log 2>&1 &

CUDA_VISIBLE_DEVICES=2,3,4,5 nohup vllm serve meta-llama/Llama-3.3-70B-Instruct \
    --served-model-name meta-llama/Llama-3.3-70B-Instruct \
    --quantization fp8 \
    --tensor-parallel-size 4 --max-model-len 65536 --max-num-seqs 16 --gpu-memory-utilization 0.92 \
    --enable-auto-tool-choice --tool-call-parser llama3_json \
    --enable-prompt-tokens-details \
    --chat-template templates/tool_chat_template_llama3.1_json.jinja \
    --port 8001 > logs/validator.log 2>&1 &

echo "reviewer  -> http://127.0.0.1:8000/v1  (logs/reviewer.log)"
echo "validator -> http://127.0.0.1:8001/v1  (logs/validator.log)"
echo "when both answer /v1/models:  uv run scr-agents --config deploy/scr_agents.baremetal.yaml llm check"

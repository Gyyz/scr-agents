#!/usr/bin/env bash
# One-time, online step: fetch both models and the vLLM tool chat templates into deploy/models and
# deploy/templates. Everything afterwards runs offline (HF_HUB_OFFLINE=1).
#
# Both repos are gated on Hugging Face: accept the licenses in the browser first, then
#   export HF_TOKEN=hf_...
set -euo pipefail
cd "$(dirname "$0")"

export HF_HOME="$PWD/models"
mkdir -p models templates

python -m pip install --quiet --upgrade "huggingface_hub[cli]"
huggingface-cli download google/gemma-4-31B-it
huggingface-cli download meta-llama/Llama-3.3-70B-Instruct

RAW=https://raw.githubusercontent.com/vllm-project/vllm/main/examples
curl -fsSL "$RAW/tool_chat_template_gemma4.jinja"         -o templates/tool_chat_template_gemma4.jinja
curl -fsSL "$RAW/tool_chat_template_llama3.1_json.jinja"  -o templates/tool_chat_template_llama3.1_json.jinja

echo "models in $HF_HOME, templates in $PWD/templates"
du -sh models

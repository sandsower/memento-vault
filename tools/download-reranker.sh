#!/usr/bin/env bash
# Pre-download the cross-encoder model for Tier 2 reranking.
# Run this once to avoid first-use download latency.
#
# Usage: ./tools/download-reranker.sh
#
# Requires: pip install huggingface_hub

set -euo pipefail

MODEL_ID="cross-encoder/ms-marco-MiniLM-L-6-v2"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/memento-vault/models/minilm-l6-v2"

echo "Downloading ${MODEL_ID} to ${CACHE_DIR}..."
mkdir -p "${CACHE_DIR}"

python3 -c "
from huggingface_hub import hf_hub_download
import os, shutil

cache_dir = '${CACHE_DIR}'
model_id = '${MODEL_ID}'

# Download ONNX model
print('  model.onnx...')
hf_hub_download(repo_id=model_id, filename='onnx/model.onnx', local_dir=cache_dir)
# Move from onnx/ subdirectory if needed
onnx_sub = os.path.join(cache_dir, 'onnx', 'model.onnx')
onnx_dst = os.path.join(cache_dir, 'model.onnx')
if os.path.exists(onnx_sub) and not os.path.exists(onnx_dst):
    shutil.move(onnx_sub, onnx_dst)

# Download tokenizer
print('  tokenizer.json...')
hf_hub_download(repo_id=model_id, filename='tokenizer.json', local_dir=cache_dir)

print('Done.')
print(f'Model cached at: {cache_dir}')
print(f'Size: {sum(os.path.getsize(os.path.join(cache_dir, f)) for f in os.listdir(cache_dir) if os.path.isfile(os.path.join(cache_dir, f))) / 1024 / 1024:.1f} MB')
"

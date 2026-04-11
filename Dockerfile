FROM python:3.12.11-slim

LABEL maintainer="memento-vault"
LABEL description="Memento Vault — persistent knowledge store for coding agents"

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m -s /bin/bash memento

WORKDIR /app

# Copy project files
COPY memento/ ./memento/
COPY hooks/ ./hooks/
COPY pyproject.toml ./

# Install Python dependencies
RUN pip install --no-cache-dir "mcp[cli]>=1.27,<2.0" onnxruntime sqlite-vec numpy tokenizers

# Download nomic-embed-text-v1.5 int8 model at build time
RUN python -c "\
from pathlib import Path; \
from urllib.request import urlretrieve; \
model_dir = Path('/app/models/nomic-embed-text-v1.5'); \
model_dir.mkdir(parents=True, exist_ok=True); \
base = 'https://huggingface.co/nomic-ai/nomic-embed-text-v1.5/resolve/main/onnx'; \
urlretrieve(f'{base}/model_quantized.onnx', model_dir / 'model_quantized.onnx'); \
urlretrieve('https://huggingface.co/nomic-ai/nomic-embed-text-v1.5/resolve/main/tokenizer.json', model_dir / 'tokenizer.json'); \
print(f'Model downloaded: {list(model_dir.iterdir())}')"

ENV MEMENTO_MODEL_CACHE_DIR=/app/models

# Create vault directory
RUN mkdir -p /vault/notes /vault/fleeting /vault/projects /vault/archive /vault/.search \
    && chown -R memento:memento /vault

# Create config directory
RUN mkdir -p /home/memento/.config/memento-vault \
    && chown -R memento:memento /home/memento/.config

USER memento

# Default vault path inside container
ENV MEMENTO_VAULT_PATH=/vault
ENV MEMENTO_TRANSPORT=streamable-http
ENV MEMENTO_HOST=0.0.0.0
ENV MEMENTO_PORT=8745
ENV PYTHONPATH=/app

EXPOSE 8745

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "\
import json, os; from urllib.request import Request, urlopen; \
body = json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'memento_status','arguments':{}}}).encode(); \
req = Request('http://localhost:8745/mcp', data=body, method='POST'); \
req.add_header('Content-Type', 'application/json'); \
req.add_header('Accept', 'application/json'); \
key = os.environ.get('MEMENTO_API_KEY', ''); \
req.add_header('Authorization', f'Bearer {key}') if key else None; \
urlopen(req, timeout=4)" || exit 1

ENTRYPOINT ["python", "-m", "memento"]

FROM python:3.12-slim

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
RUN pip install --no-cache-dir "mcp[cli]>=1.0"

# Create vault directory
RUN mkdir -p /vault/notes /vault/fleeting /vault/projects /vault/archive \
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
import os; from urllib.request import Request, urlopen; \
req = Request('http://localhost:8745/mcp'); \
key = os.environ.get('MEMENTO_API_KEY', ''); \
req.add_header('Authorization', f'Bearer {key}') if key else None; \
urlopen(req, timeout=4)" || exit 1

ENTRYPOINT ["python", "-m", "memento"]

FROM python:3.11-slim

LABEL org.opencontainers.image.title="sentinel-review"
LABEL org.opencontainers.image.description="AI-powered code review with LangGraph + Llama 3.3 70B"

# Install git (needed for GitPython / diff-aware analysis)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    patch \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy package files
COPY pyproject.toml .
COPY sentinel_review/ sentinel_review/

# Install sentinel-review (without dev extras for smaller image)
RUN pip install --no-cache-dir -e .

# GitHub Action entrypoint script
COPY .github/actions/sentinel-review/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]

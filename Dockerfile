FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285

# Install reproducible build tooling without compiler or agent-provider extras.
RUN pip install --no-cache-dir uv==0.12.9

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_LINK_MODE=copy uv sync --frozen --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src/ src/
COPY config/swaggers.yaml.example config/swaggers.yaml.example
COPY examples/ examples/
COPY sandbox/requirements.txt sandbox/requirements.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 uv sync --frozen --no-dev --no-editable

# Create runtime directories owned by the non-root application user.
RUN useradd --uid 1000 --create-home gryphon \
    && mkdir -p /app/data /app/compiled \
    && chown -R gryphon:gryphon /app/data /app/compiled

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONDONTWRITEBYTECODE=1
ENV GRYPHON_COMPILED_OUTPUT_DIR=/app/compiled
ENV GRYPHON_CACHE_DB_PATH=/app/data/cache.db
ENV GRYPHON_RUN_DB_PATH=/app/data/runs.db
ENV GRYPHON_ARTIFACT_DIR=/app/data/artifacts
ENV GRYPHON_SANDBOX_REQUIREMENTS_PATH=/app/sandbox/requirements.txt

USER 1000:1000
EXPOSE 8000

CMD ["gryphon", "serve", "--transport", "http", "--host", "0.0.0.0"]

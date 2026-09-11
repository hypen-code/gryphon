FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285

# Install system dependencies for offline compute: no compilers or download tools.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    HOME=/tmp

# Install all sandbox Python dependencies from pinned binary wheels only.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Pre-installed offline numeric wheels replace runtime model/compiler downloads.
# Create non-root user for security
RUN groupadd -g 1000 executor && useradd -r -u 1000 -g executor -s /bin/false executor

# Set workspace
WORKDIR /workspace

# Copy entrypoint
COPY entrypoint.py /workspace/entrypoint.py

# Lock down permissions
RUN chmod 444 /workspace/entrypoint.py && chmod 555 /workspace

USER 1000:1000

ENTRYPOINT ["python", "-I", "-B", "/workspace/entrypoint.py"]

# ContextCore Container Specification (Local Loopback & Diagnostic Scope)
# ContextCore is primarily designed as a local background service on desktop macOS/Linux.
# Containerized deployment operates strictly within local loopback (127.0.0.1) for testing.

# Build stage
FROM python:3.11-slim AS builder

WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy both base requirements and nested managed format requirements
COPY requirements.txt requirements-managed-formats.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Runner stage
FROM python:3.11-slim AS runner

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY app/ app/

# Run as non-root user for security
RUN useradd -u 8888 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8420

# Enforce strict loopback binding via standard CLI entrypoint
CMD ["python", "-m", "app.cli", "serve", "--host", "127.0.0.1", "--port", "8420"]

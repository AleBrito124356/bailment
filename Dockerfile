# bailment -- API, worker, reconciler and CLI in one image.
#
# One image for every role. The processes differ only in their command, and shipping four
# images that must agree about a database schema and a lease state machine is how a rolling
# deploy ends up with a worker that writes a state the API cannot read.
#
# Two stages, because the build needs a compiler toolchain and the runtime must not have
# one. Dependencies are installed in a layer of their own, before the source is copied, so
# editing a Python file does not re-resolve the dependency tree.

# --------------------------------------------------------------------------------------
# Stage 1: build the virtualenv
# --------------------------------------------------------------------------------------
FROM python:3.14-slim-bookworm AS builder

# Pinned rather than ':latest': the point of committing uv.lock is a reproducible dependency
# set, and a resolver that drifts underneath it defeats that. It must also not be pinned
# *older* than the uv that wrote the lockfile -- uv.lock declares `revision = 3`, and a uv
# that predates that revision refuses to read the file rather than silently re-resolving.
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, from the lockfile only. --no-install-project is what keeps this layer
# valid across source edits; the project itself is installed in the second sync below.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# --------------------------------------------------------------------------------------
# Stage 2: runtime
# --------------------------------------------------------------------------------------
FROM python:3.14-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="bailment" \
      org.opencontainers.image.description="A provisioning broker that hands AI agents capabilities instead of credentials, on time-boxed leases that destroy themselves." \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/AleBrito124356/bailment"

# Non-root, and it owns nothing it does not need to write. This process holds an encryption
# key and talks to cloud APIs; it has no business being able to modify its own code.
RUN groupadd --system --gid 1000 bailment \
 && useradd --system --uid 1000 --gid bailment --create-home bailment

WORKDIR /app

COPY --from=builder --chown=bailment:bailment /app/.venv /app/.venv
COPY --chown=bailment:bailment src/ ./src/
COPY --chown=bailment:bailment pyproject.toml README.md ./

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # 0.0.0.0 rather than the 127.0.0.1 default: a container that binds loopback is a
    # container nothing outside it can reach, and the symptom is an empty page rather than
    # an error anybody can act on.
    BAILMENT_HOST=0.0.0.0 \
    BAILMENT_PORT=8080

USER bailment

EXPOSE 8080

# /health/live rather than /health: liveness must not fail because the database is briefly
# unreachable. A liveness probe that follows readiness restarts every replica during a
# failover, which is how a database blip becomes an outage. urllib rather than curl, which
# this image deliberately does not carry.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u,sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8080/health/live', timeout=4).status == 200 else 1)"]

# The API only. The worker, the ticker and the reconciler are separate commands on this same
# image -- see docker-compose.yml for why the demo splits them the way it does.
ENTRYPOINT ["bailment"]
CMD ["serve"]

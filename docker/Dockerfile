# syntax=docker/dockerfile:1

# Keep the same Python minor version in the build and runtime stages.
ARG PYTHON_IMAGE=python:3.13-slim

# --- Build stage: virtual environment with the runtime dependencies ---------
FROM ${PYTHON_IMAGE} AS build
SHELL ["/bin/sh", "-eu", "-c"]
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src

# Resolved from pyproject.toml's [project].dependencies at build time,
# deliberately unpinned: every build installs the newest release of each
# dependency ("latest is greatest"). Two architecture builds started minutes
# apart can therefore resolve different transitive versions; release.yml's
# cross-arch parity check is the backstop that catches it, not a lock file.
# The application code stays in /app with its packaged catalog, administration
# templates and assets - only the dependency list is needed here, so only
# pyproject.toml is copied into this stage.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && python -c 'import tomllib; print("\n".join(tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]))' > requirements.txt \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt \
    && /opt/venv/bin/pip check

# --- Runtime stage -----------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
SHELL ["/bin/sh", "-eu", "-c"]

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RCT_API_CONTAINER=1 \
    PATH=/opt/venv/bin:$PATH

# Pick up Debian security fixes already published for the base image and install
# gosu for the entrypoint's privilege drop. The docker-clean hook is removed so
# the BuildKit cache mounts keep the packages.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv

# The service never installs anything at runtime; pip is a scanner-finding magnet.
RUN /opt/venv/bin/python -m pip uninstall -y pip \
    && /usr/local/bin/python -m pip uninstall -y pip \
    && groupadd --gid 10001 rctapi \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin rctapi

WORKDIR /app
COPY app ./app
COPY run.py pyproject.toml LICENSE CHANGELOG.md ./
COPY --chmod=755 docker/entrypoint.sh /entrypoint.sh
# Fail the build on a syntax error or CRLF line endings (the shebang would break).
RUN bash -n /entrypoint.sh \
    && ! grep -q "$(printf '\r')" /entrypoint.sh
RUN python -m compileall -q /app/run.py /app/app \
    && chmod -R a+rX,a-w /app \
    && mkdir -p /app/data \
    && chown 10001:10001 /app/data \
    && chmod 0700 /app/data
# No USER: the container starts as root so /entrypoint.sh can repair the data
# volume ownership, then drops to rctapi (10001:10001) with gosu.

# Declared last so per-build values do not invalidate the layers above.
ARG APP_VERSION="unknown"
ARG GIT_SHA="unknown"
ARG BUILD_DATE="unknown"
ENV GIT_SHA=${GIT_SHA} \
    BUILD_DATE=${BUILD_DATE}
LABEL org.opencontainers.image.title="rct-api" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.description="Vendor-neutral REST gateway for RCT Power inverters" \
      org.opencontainers.image.source="https://github.com/Gill-Bates/docker-repo" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}"

# /health is token-free. The probe targets the container's own address, not loopback, so a
# loopback BIND_ADDRESS (unreachable through any port mapping) turns the container unhealthy.
# The port follows BIND_PORT from settings.env; the image ships no HTTP client tools.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,socket,urllib.request as u; u.urlopen('http://%s:%s/health' % (socket.gethostbyname(socket.gethostname()), os.environ.get('BIND_PORT', '8080')), timeout=3)"]

# Exec form; the entrypoint ends in `exec gosu`, so the server is PID 1 and receives SIGTERM
# directly for its graceful shutdown.
ENTRYPOINT ["/entrypoint.sh"]
CMD ["serve"]

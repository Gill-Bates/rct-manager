#!/usr/bin/env bash
#
# docker/build.sh
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# =============================================================================
# rct-manager - image build
# =============================================================================
# Manual local-build path: this script's only job is to build and push the
# :dev tag of giiibates/rct-manager on Docker Hub, feeding the OCI metadata
# (version, git sha, build date) into the Dockerfile ARGs. The version comes
# from pyproject.toml. Everything else — multi-arch (linux/amd64 +
# linux/arm64) builds and the :latest/:<version> tags — is handled by the
# release pipeline (.github/workflows/release.yml), not this script.
#
# Usage (from anywhere):
#   docker/build.sh [extra buildx args...]   # build + push :dev
#   IMAGE=my.reg/repo docker/build.sh        # another repository
# =============================================================================

set -euo pipefail

for command in docker git date python3; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "ERROR: required command not found: ${command}" >&2
        exit 1
    fi
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
IMAGE="${IMAGE:-giiibates/rct-manager}"

if ! APP_VERSION="$(python3 -c '
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    version = tomllib.load(f).get("project", {}).get("version")
if not isinstance(version, str) or not version:
    raise SystemExit(1)
print(version)
' "${REPO_ROOT}/pyproject.toml")"; then
    echo "ERROR: could not read [project].version from ${REPO_ROOT}/pyproject.toml" >&2
    exit 1
fi

GIT_SHA="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo unknown)"
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "Building ${IMAGE}:dev (APP_VERSION=${APP_VERSION} GIT_SHA=${GIT_SHA} BUILD_DATE=${BUILD_DATE})" >&2

# A push's OCI revision label must match what is actually built: no
# "unknown" sha silently standing in for a resolvable one.
if [ "${GIT_SHA}" = "unknown" ] || [ -z "${GIT_SHA}" ]; then
    echo "ERROR: refusing to push with an unresolved GIT_SHA; the image's OCI revision label would be meaningless" >&2
    exit 1
fi

docker buildx build \
    --platform linux/amd64 \
    --target runtime \
    --pull \
    --build-arg APP_VERSION="${APP_VERSION}" \
    --build-arg GIT_SHA="${GIT_SHA}" \
    --build-arg BUILD_DATE="${BUILD_DATE}" \
    -f "${SCRIPT_DIR}/Dockerfile" \
    -t "${IMAGE}:dev" \
    --push \
    "$@" \
    "${REPO_ROOT}"

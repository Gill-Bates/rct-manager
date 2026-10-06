#!/usr/bin/env bash
#
# docker/build.sh
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# =============================================================================
# rct-api - image build
# =============================================================================
# Manual local-build / private-registry path: builds docker.cirrio.de/rct-api
# by default and feeds the OCI metadata (version, git sha, build date) into
# the Dockerfile ARGs. The version comes from pyproject.toml. Tagged releases
# ship separately via .github/workflows/release.yml, which builds multi-arch
# (linux/amd64 + linux/arm64) and publishes to Docker Hub as
# giiibates/rct-rest-api; this script only builds linux/amd64. By default the
# image is built and PUSHED as :latest and :<version> (requires `docker
# login`). PUSH=0 builds a local :dev image only.
#
# Usage (from anywhere):
#   docker/build.sh [extra buildx args...]   # build + push :latest and :<version>
#   PUSH=0 docker/build.sh                   # local :dev image, no push
#   IMAGE=my.reg/repo docker/build.sh        # another repository
# =============================================================================

set -euo pipefail

for command in docker git date python3; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "ERROR: required command not found: ${command}" >&2
        exit 1
    fi
done

case "${PUSH:-1}" in
    1|true|yes) PUSH=1 ;;
    0|false|no) PUSH=0 ;;
    *)
        echo "ERROR: PUSH must be one of 1,true,yes,0,false,no (got: ${PUSH})" >&2
        exit 1
        ;;
esac

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
IMAGE="${IMAGE:-docker.cirrio.de/rct-api}"

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

echo "Building ${IMAGE} (APP_VERSION=${APP_VERSION} GIT_SHA=${GIT_SHA} BUILD_DATE=${BUILD_DATE})" >&2

if [ "${PUSH}" = 1 ]; then
    # A push publishes :latest and the stable version tag, so its OCI revision
    # label must match what is actually built: no uncommitted/untracked worktree
    # content, and no "unknown" sha silently standing in for a resolvable one.
    if [ "${GIT_SHA}" = "unknown" ] || [ -z "${GIT_SHA}" ]; then
        echo "ERROR: refusing to push with an unresolved GIT_SHA; the image's OCI revision label would be meaningless" >&2
        exit 1
    fi
    if [ -n "$(git -C "${REPO_ROOT}" status --porcelain --untracked-files=all)" ]; then
        echo "ERROR: refusing to push from a dirty worktree (uncommitted or untracked changes); commit or stash first" >&2
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
        -t "${IMAGE}:latest" \
        -t "${IMAGE}:${APP_VERSION}" \
        --push \
        "$@" \
        "${REPO_ROOT}"
else
    docker build \
        --target runtime \
        --pull \
        --build-arg APP_VERSION="${APP_VERSION}" \
        --build-arg GIT_SHA="${GIT_SHA}" \
        --build-arg BUILD_DATE="${BUILD_DATE}" \
        -f "${SCRIPT_DIR}/Dockerfile" \
        -t "${IMAGE}:dev" \
        "$@" \
        "${REPO_ROOT}"
fi

#!/usr/bin/env bash
#
# Turn a checkout into a verified image (DESIGN.md §10, milestone 9 design D15).
#
#   ./build.sh                 every gate, in order; exit 0 means an image exists
#                              that passed all of them
#   ./build.sh smoke scan      only the named gates, against $IMAGE — for checking
#                              a gate itself; says loudly that it verified nothing
#
# Gates, in order, stopping at the first failure and naming it:
#   lock   uv.lock agrees with pyproject.toml (never updated here)
#   format ruff format --check — checks only, so a build never rewrites the tree
#   lint   ruff check
#   types  mypy
#   test   pytest (database tests skip without a URL, as they always have)
#   image  docker build, version from `uv version`, commit from git (-dirty if modified)
#   smoke  the image starts as a stranger (UID 52037) on a read-only root, no capabilities
#   replay every committed capture replayed inside the image (musl) renders byte for byte
#          what the same replay renders on this host — the tests never run on musl
#   scan   trivy, from a pinned image, fed a `docker save` tarball — never the socket.
#          Reports HIGH and CRITICAL findings, fixed and unfixed; it fails only when
#          the scan itself cannot run or .trivyignore breaks its rule, never on a
#          finding (operator decision, milestone 9)
#
# Needs only the project's toolchain (uv) and a container engine. The image is
# never pushed. IMAGE overrides the tag (default sighop:<version>-<commit>); the
# image is also tagged sighop:local, which compose.yaml uses by default.

set -euo pipefail
cd "$(dirname "$0")"

TRIVY_IMAGE="aquasec/trivy:0.74.0@sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969"
TRIVY_CACHE_VOLUME="sighop-trivy-cache"
SMOKE_USER="52037:52037"
LOCAL_TAG="sighop:local"

# `set -e` does not apply inside a function run as an `if` condition, which is
# how `gate` runs each one — so every step in a gate below ends in `|| return 1`.
# Without that, a failing first command is followed by a succeeding last one and
# the gate passes (found by the smoke gate's own test image).
gate() {
  local name=$1
  shift
  printf '\n==> gate: %s\n' "$name"
  if ! "$@"; then
    printf '\nbuild failed at gate: %s\n' "$name" >&2
    exit 1
  fi
}

# --- Identity of this build ---------------------------------------------------

VERSION=$(uv version --short)
COMMIT=$(git rev-parse --short=12 HEAD)
if [ -n "$(git status --porcelain)" ]; then
  # Uncommitted changes: the image says so rather than claiming the clean commit.
  COMMIT="${COMMIT}-dirty"
fi
IMAGE=${IMAGE:-sighop:${VERSION}-${COMMIT}}

# --- The gates -----------------------------------------------------------------

lock() {
  if ! uv lock --check; then
    echo "uv.lock does not agree with pyproject.toml; run \`uv lock\` and commit it (the build never updates it)" >&2
    return 1
  fi
}

format() { uv run --locked ruff format --check; }

lint() { uv run --locked ruff check; }

types() { uv run --locked mypy; }

tests() { uv run --locked pytest -q; }

image() {
  # No provenance or SBOM attestation manifests: pushed to a registry, each one
  # is a separate untagged package version, and the workflow's "keep the latest
  # three versions" would count them instead of tags.
  docker build \
    --provenance=false \
    --sbom=false \
    --build-arg "SIGHOP_VERSION=${VERSION}" \
    --build-arg "SIGHOP_COMMIT=${COMMIT}" \
    --tag "${IMAGE}" \
    --tag "${LOCAL_TAG}" \
    .
}

smoke() {
  # `cli.py` imports the web application at module level, so `--help` loads
  # FastAPI, Jinja, PyNaCl and SQLAlchemy: the whole application imports as a
  # user the image has never heard of, on a root nothing can write to.
  local constrained=(
    docker run --rm
    --read-only --tmpfs /tmp
    --user "${SMOKE_USER}"
    --cap-drop ALL
    --security-opt no-new-privileges
    --network none
  )
  "${constrained[@]}" "${IMAGE}" --help > /dev/null || return 1
  "${constrained[@]}" "${IMAGE}" run --help > /dev/null || return 1
  echo "smoke: ${IMAGE} started as ${SMOKE_USER} on a read-only root with no capabilities"
}

replay() {
  # The image is Alpine (musl) and the test suite runs on the host (glibc), so
  # this is the one place the platform's own behaviour is checked on the libc
  # it ships with. Receptions only: a replay never transmits and needs no
  # database, so both runs are told to have none.
  local capture expected actual count=0
  expected=$(mktemp) || return 1
  actual=$(mktemp) || return 1
  # shellcheck disable=SC2064
  trap "rm -f '${expected}' '${actual}'" RETURN
  for capture in captures/*.jsonl; do
    env -u DATABASE_URL -u DATABASE_URL_FILE \
      uv run --locked sighop run --replay "${capture}" --status-interval 3600 \
      > "${expected}" 2> /dev/null || return 1
    docker run --rm --read-only --tmpfs /tmp --user "${SMOKE_USER}" --cap-drop ALL \
      --security-opt no-new-privileges --network none \
      -v "${PWD}/captures:/app/captures:ro" \
      "${IMAGE}" run --replay "${capture}" --status-interval 3600 \
      > "${actual}" 2> /dev/null || return 1
    if ! cmp -s "${expected}" "${actual}"; then
      echo "replay: ${capture} renders differently inside the image:" >&2
      diff "${expected}" "${actual}" | head -20 >&2
      return 1
    fi
    count=$((count + 1))
  done
  [ "${count}" -gt 0 ] || { echo "replay: no captures to replay" >&2; return 1; }
  echo "replay: ${count} capture(s) render identically on the host and in ${IMAGE}"
}

check_trivyignore() {
  # A suppression must say why: every entry is immediately preceded by a
  # `#` comment line. Enforced here rather than by review.
  local previous="" line number=0 refused=0
  [ -f .trivyignore ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    number=$((number + 1))
    if [ -n "${line//[[:space:]]/}" ] && [[ ! $line =~ ^[[:space:]]*# ]]; then
      if [[ ! $previous =~ ^[[:space:]]*#.*[^[:space:]#] ]]; then
        echo ".trivyignore:${number}: '${line}' has no reason; put a '# why' comment on the line above it" >&2
        refused=1
      fi
    fi
    previous=$line
  done < .trivyignore
  return "$refused"
}

scan() {
  check_trivyignore || return 1
  local workdir
  workdir=$(mktemp -d) || return 1
  # shellcheck disable=SC2064
  trap "rm -rf '${workdir}'" RETURN
  docker save "${IMAGE}" -o "${workdir}/image.tar" || return 1
  touch "${workdir}/.trivyignore" || return 1
  if [ -f .trivyignore ]; then cp .trivyignore "${workdir}/.trivyignore" || return 1; fi
  chmod -R a+rX "${workdir}" || return 1
  local trivy=(
    docker run --rm
    -v "${workdir}:/scan:ro"
    -v "${TRIVY_CACHE_VOLUME}:/cache"
    "${TRIVY_IMAGE}"
    image --cache-dir /cache --input /scan/image.tar
    --ignorefile /scan/.trivyignore --scanners vuln
  )
  # --exit-code 0: findings are printed, not enforced. A non-zero exit from
  # here is trivy failing to scan (no database, unreadable tarball), and that
  # does fail the gate — an unscanned image must not look like a clean one.
  echo "scan: HIGH and CRITICAL findings, fixed and unfixed (reported, not failing the build)"
  "${trivy[@]}" --severity HIGH,CRITICAL --exit-code 0 --show-suppressed || return 1
}

run_gate() {
  case "$1" in
    lock) gate lock lock ;;
    format) gate format format ;;
    lint) gate lint lint ;;
    types) gate types types ;;
    test) gate test tests ;;
    image) gate image image ;;
    smoke) gate smoke smoke ;;
    replay) gate replay replay ;;
    scan) gate scan scan ;;
    *) echo "unknown gate: $1 (lock format lint types test image smoke replay scan)" >&2; exit 2 ;;
  esac
}

if [ "$#" -gt 0 ]; then
  for name in "$@"; do run_gate "$name"; done
  printf '\npartial build: only [%s] ran against %s — this is NOT a verified image\n' "$*" "${IMAGE}"
  exit 0
fi

gate lock lock
gate format format
gate lint lint
gate types types
gate test tests
gate image image
gate smoke smoke
gate replay replay
gate scan scan

printf '\nbuild succeeded\n  image    %s (also %s)\n  version  %s\n  commit   %s\n' \
  "${IMAGE}" "${LOCAL_TAG}" "${VERSION}" "${COMMIT}"

"""The container, compose and build files keep their hardening (task 12.5, D16).

Cheap regression protection for a line someone deletes. Whether these files
*work* is the live exercise's job, because that needs Docker and the board; this
needs neither, and no YAML library either — the checks read the files as text,
block by block, which is enough to notice a missing key and adds no dependency.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
COMPOSE = ROOT / "compose.yaml"
BUILD = ROOT / "build.sh"
TRIVYIGNORE = ROOT / ".trivyignore"


# --- A minimal reader for the compose file ----------------------------------


def _block(text: str, header: str, indent: int) -> str:
    """The lines under `header` (at `indent` spaces) until the next sibling key."""
    lines = text.splitlines()
    prefix = " " * indent
    for index, line in enumerate(lines):
        if line.rstrip() == f"{prefix}{header}:" or line.startswith(f"{prefix}{header}: "):
            body = []
            for following in lines[index + 1 :]:
                stripped = following.strip()
                if stripped and not following.startswith(" " * (indent + 1)):
                    break
                body.append(following)
            return "\n".join(body)
    raise AssertionError(f"no {header!r} block at indent {indent}")


def _without_comments(text: str) -> str:
    return "\n".join(
        line.split(" #", 1)[0] if not line.lstrip().startswith("#") else ""
        for line in text.splitlines()
    )


def _service(text: str, name: str) -> str:
    return _block(_without_comments(text), name, 2)


HARDENING = {
    "user": r"^\s+user: \"\$\{UID:\?[^}]+\}:\$\{GID:\?[^}]+\}\"",
    "read_only": r"^\s+read_only: true$",
    "tmpfs": r"^\s+tmpfs:\n\s+- /tmp$",
    "cap_drop": r"^\s+cap_drop:\n\s+- ALL$",
    "no-new-privileges": r"^\s+security_opt:\n\s+- no-new-privileges:true$",
    "init": r"^\s+init: true$",
    "stop_grace_period": r"^\s+stop_grace_period: 20s$",
    "log rotation": r"^\s+driver: json-file\n\s+options:\n(?:\s+#.*\n)*\s+max-size:",
}


def _missing_hardening(text: str, service: str) -> list[str]:
    body = _service(text, service)
    return [name for name, pattern in HARDENING.items() if not re.search(pattern, body, re.M)]


# --- 11.x The image ----------------------------------------------------------


def test_the_dockerignore_is_an_allowlist() -> None:
    lines = [
        line.strip()
        for line in DOCKERIGNORE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines[0] == "*", "everything is excluded first"
    allowed = {line[1:] for line in lines if line.startswith("!")}
    assert allowed == {"src", "alembic", "alembic.ini", "pyproject.toml", "uv.lock"}


def test_the_dockerfile_pins_its_bases_by_digest_and_installs_from_the_lock() -> None:
    text = DOCKERFILE.read_text()
    assert re.search(r"python:3\.13-alpine@sha256:[0-9a-f]{64}", text)
    assert re.search(r"ghcr\.io/astral-sh/uv:[0-9.]+@sha256:[0-9a-f]{64}", text)
    assert "uv sync --locked --no-dev --no-install-project" in text
    assert "uv sync --locked --no-dev --no-editable" in text
    instructions = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    assert "--frozen" not in instructions, "--frozen installs from a stale lock"
    assert "UV_COMPILE_BYTECODE=1" in text


def test_the_final_stage_has_no_user_the_entrypoint_and_the_build_identity() -> None:
    text = DOCKERFILE.read_text()
    final = text[text.rindex("\nFROM ") :]
    assert not re.search(r"^USER\b", final, re.M), "the image must not hardcode a user"
    assert 'ENTRYPOINT ["sighop"]' in final
    # No CMD: the entry point takes no arguments and refuses any it is given.
    assert not re.search(r"^CMD\b", final, re.M), "the image passes the node an argument"
    assert "SIGHOP_ALEMBIC_DIR=/app/alembic" in final
    assert "PYTHONDONTWRITEBYTECODE=1" in final
    assert "SIGHOP_COMMIT_HASH=${SIGHOP_COMMIT}" in final
    assert "org.opencontainers.image.revision" in final
    assert "COPY --from=build /app /app" in final
    assert not re.search(r"^COPY (src|tests|captures|keys|\.env)", final, re.M)
    build = text[: text.rindex("\nFROM ")]
    assert "chmod -R a+rX /app" in build
    assert "python -m compileall -q /app/alembic" in build


# --- 12.x The deployment -----------------------------------------------------


def test_the_deployment_is_one_service() -> None:
    services = _block(_without_comments(COMPOSE.read_text()), "services", 0)
    names = re.findall(r"^  ([a-z_-]+):\s*$", services, re.M)
    assert names == ["sighop"]


def test_the_platform_container_is_hardened() -> None:
    assert _missing_hardening(COMPOSE.read_text(), "sighop") == []


def test_the_sighop_service_reaches_the_modem_by_stable_path_with_a_numeric_group() -> None:
    body = _service(COMPOSE.read_text(), "sighop")
    assert re.search(r"group_add:\n\s+- \"\$\{DIALOUT_GID:\?", body)
    assert re.search(r"source: \"\$\{SIGHOP_MODEM:\?[^}]+\}\"\n\s+target: /dev/modem", body)
    assert re.search(r"^\s+restart: unless-stopped$", body, re.M)
    assert '- "${SIGHOP_WEB_BIND:-127.0.0.1}:${SIGHOP_WEB_PORT:-8080}:8080"' in body
    assert "SIGHOP_MODEM: /dev/modem" in body
    assert "SIGHOP_WEB_HOST: 0.0.0.0" in body
    # Compose cannot omit an entry, so the extra name defaults to one already
    # listed and the node drops the duplicate.
    assert (
        'SIGHOP_WEB_ALLOWED_HOSTS: "localhost:${SIGHOP_WEB_PORT:-8080},'
        "127.0.0.1:${SIGHOP_WEB_PORT:-8080},"
        '${SIGHOP_WEB_ALLOWED_HOST:-localhost:${SIGHOP_WEB_PORT:-8080}}"'
    ) in body
    assert "SIGHOP_ENABLE_TRANSMIT" not in body, "a fresh deployment is receive-only"
    assert "privileged" not in body


def test_every_setting_is_an_environment_variable_and_there_is_no_command() -> None:
    """`compose-deployment`: the node migrates by starting, so there is nothing
    to ask for — and it takes no arguments, so there is nothing to pass."""
    body = _service(_without_comments(COMPOSE.read_text()), "sighop")
    assert not re.search(r"^\s+(command|entrypoint):", body, re.M)
    assert re.search(r"^\s+environment:$", body, re.M)


def test_the_database_is_external_and_one_file_serves_every_host() -> None:
    text = _without_comments(COMPOSE.read_text())
    assert not re.search(r"\bpostgres\b", text), "a database service or image"
    for fragment in ("pgdata", "POSTGRES_PASSWORD", "depends_on"):
        assert fragment not in text, fragment
    assert not re.search(r"internal:\s*true", text)
    assert not re.search(r"^(volumes|networks):", text, re.M)
    assert not (ROOT / "compose.dev.yaml").exists(), "an environment-specific override"


def test_secrets_come_from_the_gitignored_env_file_and_are_never_values() -> None:
    text = COMPOSE.read_text()
    body = _service(text, "sighop")
    assert 'DATABASE_URL: "${DATABASE_URL:?' in body
    assert 'SIGHOP_SECRET_KEY: "${SIGHOP_SECRET_KEY:?' in body
    assert ".env" in (ROOT / ".gitignore").read_text().splitlines()
    assert not re.search(r"postgresql\+asyncpg://[^:\s]+:[^@\s$]+@", text), "a URL with a password"
    assert not re.search(r"^\s+DATABASE_URL:[ ]*[^\s\"$]", text, re.M)
    assert not re.search(r"^\s+SIGHOP_SECRET_KEY:[ ]*[^\s\"$]", text, re.M)


@pytest.mark.parametrize(
    ("key", "line"),
    [("read_only", "    read_only: true\n"), ("cap_drop", "    cap_drop:\n      - ALL\n")],
)
def test_deleting_a_hardening_key_is_noticed(key: str, line: str) -> None:
    """12.5: the check is not vacuous — remove the key and it fails."""
    text = COMPOSE.read_text()
    assert line in text
    mutated = text.replace(line, "", 1)
    assert key in _missing_hardening(mutated, "sighop")


# --- 13.x The build script ---------------------------------------------------


def test_the_build_script_runs_its_gates_in_order() -> None:
    text = BUILD.read_text()
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    order = [
        text.index(f"gate {name} ")
        for name in ("lock", "lint", "types", "test", "image", "smoke", "replay", "scan")
    ]
    assert order == sorted(order), "the gates run out of order"
    assert "build failed at gate:" in text


def test_the_scanner_is_pinned_and_never_given_the_docker_socket() -> None:
    text = BUILD.read_text()
    assert re.search(r"aquasec/trivy:[0-9.]+@sha256:[0-9a-f]{64}", text)
    assert "docker.sock" not in text
    assert "docker save" in text
    assert "--severity HIGH,CRITICAL" in text
    assert "--show-suppressed" in text
    # Report, never enforce: findings print and the build carries on.
    assert "--exit-code 0" in text
    assert "--exit-code 1" not in text and "--ignore-unfixed" not in text


def test_the_smoke_run_is_a_stranger_on_a_read_only_root() -> None:
    text = BUILD.read_text()
    for flag in ("--read-only --tmpfs /tmp", '--user "${SMOKE_USER}"', "--cap-drop ALL"):
        assert flag in text
    assert 'SMOKE_USER="52037:52037"' in text
    smoke = text[text.index("smoke() {") : text.index("replay() {")]
    # Imports the whole application, then requires the real entry point to
    # refuse an empty environment and say how to make the secret.
    assert '--entrypoint python "${IMAGE}"' in smoke and "import sighop.boot" in smoke
    assert 'if refusal=$("${constrained[@]}" "${IMAGE}" 2>&1); then' in smoke
    assert 'grep -qF "openssl rand -base64 32"' in smoke


def test_the_replay_gate_compares_the_image_with_the_host_byte_for_byte() -> None:
    text = BUILD.read_text()
    replay = text[text.index("replay() {") : text.index("check_trivyignore() {")]
    assert "tests/corpus/*.jsonl" in replay
    assert '-v "${PWD}/tests/corpus:/app/tests/corpus:ro"' in replay
    assert "captures/" not in replay.replace("captures/ is gitignored", ""), (
        "the gate must replay the generated corpus, never a live recording"
    )
    assert "cmp -s" in replay
    assert "uv run --locked python -m sighop.replay" in replay
    assert '--entrypoint python "${IMAGE}" -m sighop.replay' in replay
    assert '--user "${SMOKE_USER}"' in replay and "--read-only" in replay
    assert "--network none" in replay


def test_the_final_stage_runs_nothing_and_deletes_nothing() -> None:
    """Deleting base-image files in a later layer saves no bytes and hides them
    from the scan while still shipping them — so the final stage only copies."""
    final = DOCKERFILE.read_text()[DOCKERFILE.read_text().rindex("\nFROM ") :]
    instructions = [
        line for line in final.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    assert not any(line.startswith("RUN") for line in instructions), (
        "the final stage runs a command"
    )
    assert not any("rm -" in line for line in instructions)


def test_the_committed_trivyignore_states_its_rule() -> None:
    text = TRIVYIGNORE.read_text()
    assert text.startswith("#")
    entries = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
    assert entries == [], "no finding is suppressed in the committed file"


# --- The GitHub Actions workflow ---------------------------------------------

WORKFLOW = ROOT / ".github" / "workflows" / "build.yml"


def test_the_workflow_runs_build_sh_and_pins_every_action_by_commit() -> None:
    text = WORKFLOW.read_text()
    # Only the image gate: the others run locally, not in CI (ci-image-build-only).
    assert re.findall(r"run: \./build\.sh\b.*", text) == ["run: ./build.sh image"]
    uses = re.findall(r"uses: (\S+)", text)
    assert uses, "no actions found"
    for action in uses:
        assert re.search(r"@[0-9a-f]{40}$", action), f"{action} is not pinned to a commit"
    assert "Sigurs/container-rebuilds/.github/actions/notify-discord@" in text
    assert "secrets.DISCORD_WEBHOOK" in text


def test_the_workflow_tags_by_commit_and_time_and_keeps_three_versions() -> None:
    text = WORKFLOW.read_text()
    assert 'tag="$(git rev-parse --short=12 HEAD)-$(date -u +%Y%m%d-%H%M%S)"' in text
    assert "min-versions-to-keep: 3" in text
    assert "delete-only-untagged-versions: false" in text
    # Pull requests only build; only pushes publish, notify and prune.
    for step in ("Log in to GHCR", "Push", "Notify Discord", "Keep only the newest three versions"):
        block = text[text.index(f"- name: {step}") :].split("\n      - ", 1)[0]
        assert "if: github.event_name != 'pull_request'" in block, step


def test_one_push_is_one_package_version() -> None:
    """Attestation manifests would be counted as versions by the retention step."""
    text = BUILD.read_text()
    image = text[text.index("image() {") : text.index("smoke() {")]
    assert "--provenance=false" in image and "--sbom=false" in image


def test_the_workflow_starts_no_database() -> None:
    """No gate that runs in CI needs one, so none is started (ci-image-build-only)."""
    text = WORKFLOW.read_text()
    assert not re.search(r"^\s+services:", text, re.M)
    assert "SIGHOP_TEST_DATABASE_URL" not in text

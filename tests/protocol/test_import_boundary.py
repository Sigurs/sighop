"""The protocol layer's dependency boundary, enforced (DESIGN.md §11).

`protocol/` is "pure functions over bytes" with no dependency on `radio/`,
`db/` or `net/`, and no I/O of any kind. That is a property of the source, so
it is checked against the source: a static scan of every import statement in
every `sighop.protocol` module, which cannot be fooled by a module that happens
not to have been imported yet at test time.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import sighop.protocol

FORBIDDEN_ROOTS = frozenset(
    {
        "sighop.radio",
        "sighop.db",
        "sighop.net",
        "sighop.web",
        "asyncio",
        "serial",
        "serial_asyncio",
        # Milestone 8's packages, named for the same reason the database drivers
        # are: an import of `fastapi` inside `protocol/` breaks the layering just
        # as surely without ever mentioning `sighop.web`.
        "fastapi",
        "starlette",
        "uvicorn",
        "jinja2",
    }
)

WEB_ROOTS = ("sighop.web", "fastapi", "starlette", "uvicorn", "jinja2")

PROTOCOL_DIR = Path(sighop.protocol.__file__).parent


def _module_files() -> list[Path]:
    files = sorted(PROTOCOL_DIR.glob("*.py"))
    assert files, f"no protocol modules found under {PROTOCOL_DIR}"
    return files


def _imported_names(source: str, module_name: str) -> set[str]:
    """Every module named by an import statement, as a dotted absolute name."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import, resolves inside sighop.protocol
                continue
            if node.module is not None:
                names.add(node.module)
                names.update(f"{node.module}.{a.name}" for a in node.names)
    return {name for name in names if not name.startswith(f"{module_name}.")}


def _violations(names: set[str]) -> set[str]:
    return {
        name
        for name in names
        for root in FORBIDDEN_ROOTS
        if name == root or name.startswith(f"{root}.")
    }


@pytest.mark.parametrize("path", _module_files(), ids=lambda p: p.name)
def test_module_imports_nothing_below_or_beside_it(path: Path) -> None:
    names = _imported_names(path.read_text(), f"sighop.protocol.{path.stem}")
    assert not _violations(names), (
        f"{path.name} imports {sorted(_violations(names))}, which DESIGN.md §11 "
        "forbids in the protocol layer"
    )


def test_protocol_imports_nothing_from_the_database_layer() -> None:
    """Milestone 5 design D10, asserted rather than inferred.

    `sighop.db` is in `FORBIDDEN_ROOTS` above and was there before the package
    existed, which means the rule has so far been kept by `db/` not existing.
    Now that it does — and now that it imports SQLAlchemy, asyncpg and Alembic —
    this says so explicitly, and names the driver packages too: an import of
    `sqlalchemy` inside `protocol/` would break the layering just as surely
    without ever mentioning `sighop.db`.
    """
    database_roots = ("sighop.db", "sqlalchemy", "asyncpg", "alembic")
    offenders: dict[str, list[str]] = {}
    for path in _module_files():
        names = _imported_names(path.read_text(), f"sighop.protocol.{path.stem}")
        found = sorted(
            name
            for name in names
            for root in database_roots
            if name == root or name.startswith(f"{root}.")
        )
        if found:
            offenders[path.name] = found
    assert not offenders, (
        f"{offenders} reach the database layer from protocol/, which DESIGN.md §11 "
        "forbids: `db/` is a peer of `net/`, not a layer beneath `protocol/`"
    )


def test_protocol_imports_nothing_from_the_web_layer() -> None:
    """Milestone 8's half of the same rule.

    `web/` is the second renderer, on the far side of the layering from
    `protocol/`: the codec is pure functions over bytes and gains nothing from
    milestone 8. Naming the framework packages as well as `sighop.web` is what
    makes that checkable — the way a web dependency actually arrives in a codec
    module is a stray `from fastapi import ...` for a type, not an import of
    this project's own package.
    """
    offenders: dict[str, list[str]] = {}
    for path in _module_files():
        names = _imported_names(path.read_text(), f"sighop.protocol.{path.stem}")
        found = sorted(
            name
            for name in names
            for root in WEB_ROOTS
            if name == root or name.startswith(f"{root}.")
        )
        if found:
            offenders[path.name] = found
    assert not offenders, (
        f"{offenders} reach the web layer from protocol/, which DESIGN.md §11 forbids: "
        "`web/` renders what `protocol/` decodes and the dependency runs one way"
    )


def test_the_boundary_test_would_catch_a_deliberate_web_import() -> None:
    """The web half of the check, only worth having if it fails when it should."""
    source = (
        "from fastapi import APIRouter\n"
        "import uvicorn\n"
        "from sighop.web.serialize import rx_record\n"
    )
    names = _imported_names(source, "sighop.protocol.packet")
    assert _violations(names) == {
        "fastapi",
        "fastapi.APIRouter",
        "uvicorn",
        "sighop.web.serialize",
        "sighop.web.serialize.rx_record",
    }


def test_the_boundary_test_would_catch_a_deliberate_database_import() -> None:
    """The check above is only worth having if it fails when it should."""
    source = "from sighop.db.models import Contact\nimport sqlalchemy\n"
    names = _imported_names(source, "sighop.protocol.packet")
    assert _violations(names) == {"sighop.db.models", "sighop.db.models.Contact"}
    assert "sqlalchemy" in names


def test_protocol_package_imports_with_no_device_or_event_loop() -> None:
    """The `packet-codec` spec's isolation scenario: import and decode with no
    serial device, no database and no event loop present.
    """
    from sighop.protocol import packet

    decoded = packet.decode(bytes.fromhex("0600425431382924f0af5742afb44129753844ea8a8e"))
    assert isinstance(decoded, packet.Packet)
    assert decoded.payload_type is packet.PayloadType.RESPONSE
    assert decoded.route_type is packet.RouteType.DIRECT

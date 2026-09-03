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
        "asyncio",
        "serial",
        "serial_asyncio",
    }
)

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


def test_protocol_package_imports_with_no_device_or_event_loop() -> None:
    """The `packet-codec` spec's isolation scenario: import and decode with no
    serial device, no database and no event loop present.
    """
    from sighop.protocol import packet

    decoded = packet.decode(bytes.fromhex("0600425431382924f0af5742afb44129753844ea8a8e"))
    assert isinstance(decoded, packet.Packet)
    assert decoded.payload_type is packet.PayloadType.RESPONSE
    assert decoded.route_type is packet.RouteType.DIRECT

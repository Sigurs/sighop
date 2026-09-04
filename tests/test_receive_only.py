"""Receive-only, checked against the source rather than asserted in prose.

The property this file enforces changed at milestone 3, and the change is worth
stating precisely. Milestones 0-2 sent no `Data` frame from anywhere; milestone
3 builds the whole transmit path and gates it, so the static rule becomes:

* `Data` (type 0x00) may be sent from **exactly one place** — `Modem.send_packet`
  — which is the choke point the scheduler's receive-only gate sits in front of.
  Whether a run may reach it is a runtime property, asserted under load in
  `tests/test_tx.py` (design D6, task 5.12).
* Every `SetHardware` sub-command `radio/` requests must still be a query or the
  already-exercised `SetRadio`.
* `SetTxPower` (0x0A) and `Reboot` (0x18) must appear nowhere at all. Nothing in
  this milestone changes transmit power or restarts the board, and the surest
  way not to is not to name them.

Static, for the same reason `tests/protocol/test_import_boundary.py` is: a
runtime check only covers the paths a test happens to take.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import sighop.radio

RADIO_DIR = Path(sighop.radio.__file__).parent

# The only sub-commands this milestone is allowed to send. `SetRadio` writes
# configuration; everything else is a read.
ALLOWED_SUB_COMMANDS = frozenset(
    {
        "SUB_SET_RADIO",
        "SUB_GET_RADIO",
        "SUB_GET_TX_POWER",
        "SUB_GET_AIRTIME",
        "SUB_GET_VERSION",
        "SUB_GET_BATTERY",
        "SUB_GET_MCU_TEMP",
        "SUB_GET_SENSORS",
        "SUB_GET_DEVICE_NAME",
    }
)

# SetTxPower and Reboot: never sent, so never even named as constants.
FORBIDDEN_SUB_COMMAND_VALUES = frozenset({0x0A, 0x18})


def _module_files() -> list[Path]:
    files = sorted(RADIO_DIR.glob("*.py"))
    assert files, f"no radio modules found under {RADIO_DIR}"
    return files


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == name
    ]


DATA_SENDER = ("modem.py", "send_packet")
"""The one function allowed to put a `Data` frame on the wire."""


def _enclosing_function(tree: ast.AST, target: ast.Call) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and any(
            inner is target for inner in ast.walk(node)
        ):
            return node.name
    return None


@pytest.mark.parametrize("path", _module_files(), ids=lambda p: p.name)
def test_data_frames_are_sent_from_exactly_one_function(path: Path) -> None:
    tree = ast.parse(path.read_text())
    for call in _calls(tree, "send"):
        first = call.args[0] if call.args else None
        assert isinstance(first, ast.Name), f"{path.name}:{call.lineno} sends an unnamed type"
        if first.id == "TYPE_SET_HARDWARE":
            continue
        assert first.id == "TYPE_DATA", (
            f"{path.name}:{call.lineno} sends a frame type that is neither "
            "TYPE_SET_HARDWARE nor TYPE_DATA"
        )
        assert (path.name, _enclosing_function(tree, call)) == DATA_SENDER, (
            f"{path.name}:{call.lineno} sends a Data frame from outside "
            f"{DATA_SENDER[0]}:{DATA_SENDER[1]}; transmission has exactly one "
            "choke point so the receive-only gate has exactly one place to sit"
        )


def test_only_one_data_send_exists_in_the_whole_radio_layer() -> None:
    sends = 0
    for path in _module_files():
        tree = ast.parse(path.read_text())
        for call in _calls(tree, "send"):
            first = call.args[0] if call.args else None
            if isinstance(first, ast.Name) and first.id == "TYPE_DATA":
                sends += 1
    assert sends == 1, f"{sends} Data sends in radio/; the gate guards one call site"


@pytest.mark.parametrize("path", _module_files(), ids=lambda p: p.name)
def test_every_requested_sub_command_is_a_query_or_set_radio(path: Path) -> None:
    tree = ast.parse(path.read_text())
    for call in _calls(tree, "request"):
        first = call.args[0] if call.args else None
        if isinstance(first, ast.Name) and first.id == "sub_command":
            continue  # the generic API itself; its callers are what matter
        assert isinstance(first, ast.Name) and first.id in ALLOWED_SUB_COMMANDS, (
            f"{path.name}:{call.lineno} requests a sub-command outside the "
            f"receive-only set {sorted(ALLOWED_SUB_COMMANDS)}"
        )


@pytest.mark.parametrize("path", _module_files(), ids=lambda p: p.name)
def test_set_tx_power_and_reboot_are_not_even_named(path: Path) -> None:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            value = node.value.value
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            assert not (
                value in FORBIDDEN_SUB_COMMAND_VALUES
                and any(name.startswith("SUB_") for name in targets)
            ), (
                f"{path.name}:{node.lineno} defines a sub-command constant for "
                "SetTxPower or Reboot, which nothing in this milestone may send"
            )

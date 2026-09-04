"""Receive-only, checked against the source rather than asserted in prose.

Milestones 0-3 transmit nothing. The probe added in milestone 2 is the first
code that *writes* to the modem, so "receive-only" stops being self-evident
from the absence of a TX path and becomes a property worth enforcing: every
frame `radio/` sends must be a query or the already-exercised `SetRadio`, and
`Data` (type 0x00), `SetTxPower` (0x0A) and `Reboot` (0x18) must appear
nowhere.

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


@pytest.mark.parametrize("path", _module_files(), ids=lambda p: p.name)
def test_no_radio_module_sends_a_data_frame(path: Path) -> None:
    tree = ast.parse(path.read_text())
    for call in _calls(tree, "send"):
        first = call.args[0] if call.args else None
        assert isinstance(first, ast.Name) and first.id == "TYPE_SET_HARDWARE", (
            f"{path.name}:{call.lineno} sends a frame whose type is not "
            "TYPE_SET_HARDWARE; queueing a packet for radio transmission is "
            "out of scope until milestone 4"
        )


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

"""The web package's shape, and its silence at import time.

`sighop.web` is imported by `cli.py` to build a service and by tests that never
serve anything. Importing it must therefore do nothing: no listener, no event
loop, no template environment, no server. That is asserted in a subprocess with
sockets and loop construction booby-trapped, because the interesting failure —
a module-level `uvicorn.Server(...)` or a `Jinja2Templates(...)` that walks the
filesystem — would otherwise be invisible from inside a test session that has
already imported half the project.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import sighop.web

WEB_DIR = Path(sighop.web.__file__).parent

GUARD = """
import socket, sys

class _RefusedSocket(socket.socket):
    def __init__(self, *args, **kwargs):
        raise AssertionError("importing sighop.web opened a socket")

socket.socket = _RefusedSocket
socket.create_server = lambda *a, **k: (_ for _ in ()).throw(
    AssertionError("importing sighop.web created a server")
)

import asyncio

def _refused_loop(*args, **kwargs):
    raise AssertionError("importing sighop.web created an event loop")

asyncio.new_event_loop = _refused_loop
asyncio.run = _refused_loop

import sighop.web
import sighop.web.routes

started = sorted(m for m in sys.modules if m.split(".")[0] in {"uvicorn", "fastapi"})
print("MODULES:" + ",".join(started))
"""


def test_the_package_has_the_modules_and_asset_trees_the_design_names() -> None:
    for module in ("app.py", "state.py", "feed.py", "serialize.py"):
        assert (WEB_DIR / module).is_file(), f"web/{module} is missing"
    for tree in ("routes", "templates", "static"):
        assert (WEB_DIR / tree).is_dir(), f"web/{tree}/ is missing"
    assert (WEB_DIR / "routes" / "__init__.py").is_file()


def test_importing_the_package_starts_no_server_and_opens_no_socket() -> None:
    result = subprocess.run(
        [sys.executable, "-c", GUARD],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    modules = result.stdout.strip().removeprefix("MODULES:")
    assert modules == "", (
        f"importing sighop.web pulled in {modules}; the package's own import must "
        "stay free of the web framework so it costs nothing to reference"
    )

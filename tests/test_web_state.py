"""The runtime's service slot and the read seam (design D2).

Two claims, and both are about the shape of the project rather than about what
any page shows:

* **A service is carried, not depended on.** The radio is the run. A service the
  run is asked to carry is started with it and cancelled with it, and a service
  that fails is a service that is gone — never a run that is.
* **The seam has no cycle in it.** `runtime.py` imports nothing from `web/`,
  `web/` imports nothing from `runtime.py`, and `cli.py` is the one module that
  knows both. That is what makes it possible to drive every page from a stub,
  which is the property the whole panel's testability rests on.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

import sighop.runtime
import sighop.web
from sighop.runtime import Runtime, RuntimeConfig
from sighop.web.state import DurableState, LiveState, PanelState
from tests.webfixtures import (
    StubState,
    as_panel_state,
    authenticator,
    signed_client,
    stub_state,
)


class RecordingLogger:
    """Enough of a logger to read back what a failure said."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def info(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))

    def error(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))

    def warning(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))

    def debug(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))

    def named(self, event: str) -> list[dict[str, object]]:
        return [fields for name, fields in self.events if name == event]


async def _empty_source():
    """A source that never yields and never ends, so the run stops on `stop()`."""
    await asyncio.Event().wait()
    yield  # pragma: no cover - unreachable, and the function must be a generator


async def _startup() -> str:
    return "startup"


def _runtime(
    *services, logger: RecordingLogger | None = None, out=None
) -> Runtime:
    import io

    return Runtime(
        source=_empty_source(),
        startup=_startup,
        config=RuntimeConfig(status_interval=3600.0, advert_tick=3600.0),
        out=out or io.StringIO(),
        logger=logger or RecordingLogger(),
        services=tuple(services),
    )


# --- 6.1 The service slot ---------------------------------------------------


async def test_an_attached_service_runs_and_stops_with_the_run() -> None:
    """6.1: started alongside the runtime's own tasks, cancelled on stop."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def service() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    runtime = _runtime(service)
    run = asyncio.create_task(runtime.run())
    await asyncio.wait_for(started.wait(), timeout=2)

    runtime.stop()
    await asyncio.wait_for(run, timeout=5)

    assert cancelled.is_set(), "stopping the run did not stop the service"


async def test_a_service_that_fails_is_reported_and_does_not_kill_the_run() -> None:
    """6.1: the radio is the run; an interface failing loses the interface."""
    logger = RecordingLogger()
    failed = asyncio.Event()

    async def service() -> None:
        failed.set()
        raise RuntimeError("the port went away")

    runtime = _runtime(service, logger=logger)
    run = asyncio.create_task(runtime.run())
    await asyncio.wait_for(failed.wait(), timeout=2)
    await asyncio.sleep(0)

    assert not run.done(), "a failing service took the run down with it"

    runtime.stop()
    await asyncio.wait_for(run, timeout=5)

    reported = logger.named("service_failed")
    assert reported, "a service failed silently"
    assert "the port went away" in str(reported[0]["error"])
    assert reported[0]["outcome"] == "error"


async def test_a_run_with_no_services_is_a_run_with_no_services() -> None:
    """6.1: the slot is empty by default and costs a run that wants none nothing."""
    runtime = _runtime()
    assert runtime.services == ()

    run = asyncio.create_task(runtime.run())
    await asyncio.sleep(0)
    runtime.stop()
    await asyncio.wait_for(run, timeout=5)


# --- 6.2 The read seam ------------------------------------------------------


async def test_the_runtime_satisfies_the_panel_state_structurally() -> None:
    """6.2: without knowing the protocol exists, which is the point of it.

    Async because building a `Runtime` subscribes to the bus, which needs a
    running loop — the same reason the panel is served from inside the run.
    """
    runtime = _runtime()
    state: PanelState = runtime  # checked by mypy, not only at run time
    assert isinstance(state, LiveState)
    assert isinstance(state, DurableState)
    assert state.persistence is None
    assert state.scheduler is runtime.scheduler
    assert state.pipeline is runtime.pipeline


def test_a_stub_satisfies_the_panel_state_with_no_runtime_present() -> None:
    """6.2: the seam is a seam only if something other than `Runtime` fits it."""
    stub: StubState = stub_state()
    state = as_panel_state(stub)

    assert isinstance(state, PanelState)
    assert state.scheduler.transmit_enabled is False
    assert state.pipeline.paths.destination_count == 0
    assert len(state.contacts) == 0
    assert state.rooms == []
    assert state.radio is None
    assert state.probe_result is None
    assert state.persistence is None


# --- 6.3 No cycle between the runtime and the panel -------------------------


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _reaches(names: set[str], root: str) -> set[str]:
    return {name for name in names if name == root or name.startswith(f"{root}.")}


RUNTIME_FILE = Path(sighop.runtime.__file__)
WEB_DIR = Path(sighop.web.__file__).parent


def test_the_runtime_imports_nothing_from_the_panel() -> None:
    """6.3, design D2: the service slot takes a callable, never a `WebServer`."""
    assert not _reaches(_imports(RUNTIME_FILE), "sighop.web")


@pytest.mark.parametrize(
    "path", sorted(WEB_DIR.rglob("*.py")), ids=lambda p: p.name
)
def test_no_web_module_imports_the_runtime(path: Path) -> None:
    """6.3: the panel reads a `Protocol`, so it never learns what fills it."""
    assert not _reaches(_imports(path), "sighop.runtime"), (
        f"{path.name} imports sighop.runtime; the state the panel reads is "
        "described by web/state.py's protocols, which exist to avoid exactly this"
    )


def test_the_cli_is_the_only_module_that_knows_both() -> None:
    """6.3: one module composes them, and it is the one that already composes
    everything else."""
    package = RUNTIME_FILE.parent
    both: list[str] = []
    for path in sorted(package.rglob("*.py")):
        names = _imports(path)
        if _reaches(names, "sighop.web") and _reaches(names, "sighop.runtime"):
            both.append(str(path.relative_to(package)))
    assert both == ["cli.py"], f"{both} know both sides of the seam"


def test_a_stub_drives_every_page_with_no_runtime_present() -> None:
    """6.2: the seam's whole purpose, asserted over the route table.

    Not "a page renders" but *every* page renders, enumerated — because the way
    this stops being true is a new page reaching for something only a `Runtime`
    has, and that page would be the one nobody thought to add a test for.
    """

    from sighop.web.app import allowed_hosts, create_app
    from tests.webfixtures import safe_pages

    state = stub_state(stub_names=("panel-identity",))
    app = create_app(state, auth=authenticator(), hosts=allowed_hosts("127.0.0.1", 8080), logger=RecordingLogger())
    pages = safe_pages(app)
    assert len(pages) > 5, f"only {pages} were enumerated; the router walk is broken"

    with signed_client(app, base_url="http://127.0.0.1:8080") as client:
        for path in pages:
            assert client.get(path).status_code == 200, f"{path} needs a Runtime"

"""What every route is given, and how it gets it.

One object hung off `app.state`, rather than a dependency per value: the panel
is a single reader of a single running platform, and threading six things
through every handler would be ceremony over a fact that does not change during
a run.

It lives in its own module so `web/routes/*` can import it without importing
`web/app.py`, which imports them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from sighop.db.persistence import Persistence
from sighop.logging import Logger
from sighop.web.chat import ConversationLog
from sighop.web.feed import FeedHub
from sighop.web.guarded import NonceStore
from sighop.web.render import (
    REGULATORY_NOTE,
    meter_for,
    persistence_view,
)
from sighop.web.state import PanelState


@dataclass(slots=True)
class Panel:
    """The panel's own context: the platform, the templates and the token."""

    state: PanelState
    templates: Jinja2Templates
    token: str
    logger: Logger
    feed: FeedHub | None = None
    nonces: NonceStore = field(default_factory=NonceStore)
    chat: ConversationLog = field(default_factory=ConversationLog)
    """This run's own view of its conversations, which is the whole of chat on
    a run with no database and the live half of it on a run with one."""

    # --- Rendering ----------------------------------------------------------

    def context(self, **extra: Any) -> dict[str, Any]:
        """What every page is given, whatever else it also reads.

        The duty-cycle meter, the transmit gate and the persistence state are on
        every page by construction (design D12), so they are assembled here
        rather than remembered per route. The provenance token rides along for
        the same reason: a page that forgot it would be a page whose forms
        silently stopped working.
        """
        status = self.state.scheduler.status()
        return {
            "state": self.state,
            "scheduler": self.state.scheduler,
            "status": status,
            "meter": meter_for(status),
            "durability": persistence_view(self.state.persistence),
            "regulatory_note": REGULATORY_NOTE,
            "persistence": self.state.persistence,
            "token": self.token,
            **extra,
        }

    def page(
        self, request: Request, name: str, *, status_code: int = 200, **extra: Any
    ) -> HTMLResponse:
        return self.templates.TemplateResponse(
            request=request,
            name=name,
            context=self.context(**extra),
            status_code=status_code,
        )

    # --- Durable state ------------------------------------------------------

    @property
    def persistence(self) -> Persistence | None:
        return self.state.persistence

    @property
    def has_database(self) -> bool:
        return self.state.persistence is not None

    @property
    def degraded(self) -> bool:
        return self.state.persistence is not None and self.state.persistence.degraded


def panel(request: Request) -> Panel:
    """The panel context for this request. One object, hung off the app."""
    return request.app.state.panel  # type: ignore[no-any-return]

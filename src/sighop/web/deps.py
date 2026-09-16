"""What every route is given, and how it gets it.

One object hung off `app.state`, rather than a dependency per value: the panel
is a single reader of a single running platform, and threading six things
through every handler would be ceremony over a fact that does not change during
a run.

It lives in its own module so `web/routes/*` can import it without importing
`web/app.py`, which imports them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from sighop.db.persistence import Persistence
from sighop.logging import Logger
from sighop.web.auth import Authenticator, Session
from sighop.web.chat import ChannelLog, ConversationLog
from sighop.web.feed import FeedHub
from sighop.web.guard import client_address, current_session
from sighop.web.guarded import NonceStore, audit
from sighop.web.render import (
    REGULATORY_NOTE,
    meter_for,
    persistence_view,
)
from sighop.web.state import PanelState


@dataclass(slots=True)
class Panel:
    """The panel's own context: the platform, the templates and who may use it."""

    state: PanelState
    templates: Jinja2Templates
    auth: Authenticator
    logger: Logger
    feed: FeedHub | None = None
    sealing_secret: bytes | None = None
    """`SIGHOP_SECRET_KEY`, handed in by `cli.py` (design D1).

    The panel needs it to *open* a stored seed, which is what an export is. It
    is not panel state and is never rendered, logged or carried into an event;
    `None` is an ordinary answer — a run with no database has nothing sealed —
    and the pages that need it say so rather than failing.
    """

    nonces: NonceStore = field(default_factory=NonceStore)
    chat: ConversationLog = field(default_factory=ConversationLog)
    """This run's own view of its conversations — the live half of chat, beside
    the durable history."""

    channel_log: ChannelLog = field(default_factory=ChannelLog)
    """This run's own view of its channels, the same shape for channels."""

    announce: Callable[[str], None] | None = None
    """Writes a line to the run's own output. `cli.py` hands in the runtime's,
    so a guarded action taken in the browser is seen by an operator watching the
    terminal, naming the account that took it (`runtime-cli`)."""

    # --- Rendering ----------------------------------------------------------

    def context(self, request: Request, **extra: Any) -> dict[str, Any]:
        """What every page is given, whatever else it also reads.

        The duty-cycle meter, the transmit gate and the persistence state are on
        every page by construction (design D12), so they are assembled here
        rather than remembered per route. The provenance token rides along for
        the same reason: a page that forgot it would be a page whose forms
        silently stopped working. It is the signed-in session's own token
        (milestone 9 design D6), and the signed-in username is beside it for the
        sign-out control.
        """
        session = current_session(request)
        status = self.state.scheduler.status()
        return {
            "state": self.state,
            "scheduler": self.state.scheduler,
            "status": status,
            "meter": meter_for(status),
            "durability": persistence_view(self.state.persistence),
            "regulatory_note": REGULATORY_NOTE,
            "persistence": self.state.persistence,
            "token": "" if session is None else session.csrf_token,
            "signed_in_as": None if session is None else session.username,
            **extra,
        }

    def page(
        self, request: Request, name: str, *, status_code: int = 200, **extra: Any
    ) -> HTMLResponse:
        return self.templates.TemplateResponse(
            request=request,
            name=name,
            context=self.context(request, **extra),
            status_code=status_code,
        )

    # --- Who is asking -----------------------------------------------------

    @staticmethod
    def session(request: Request) -> Session:
        """The signed-in session. Every non-public route has one by construction.

        The guard refuses a request without a session before any handler runs,
        so reaching this without one is a route that was made public by mistake
        — which fails loudly here rather than serving as nobody.
        """
        session = current_session(request)
        if session is None:  # pragma: no cover - the guard makes this unreachable
            raise RuntimeError("a non-public route was reached without a session")
        return session

    @staticmethod
    def actor(request: Request) -> str:
        return Panel.session(request).username

    @staticmethod
    def client(request: Request) -> str:
        return client_address(request.scope)

    async def reauthenticate(
        self,
        request: Request,
        *,
        action: str,
        target: str,
        password: str | None,
        title: str,
        **fields: object,
    ) -> HTMLResponse | None:
        """The password step of a guarded action, or the refusal page (design D7).

        Called after the nonce is spent. `None` means the action may proceed.
        A refusal is the action's own event, naming the account, and a page that
        says why without saying whether the account exists or the password was
        close.
        """
        session = self.session(request)
        result = await self.auth.reauthenticate(session, password, client=self.client(request))
        if result.ok:
            return None
        audit(
            self.logger,
            action=action,
            target=target,
            outcome="refused",
            actor=session.username,
            reason=result.reason,
            **fields,
        )
        return self.page(
            request,
            "admin/refused.html",
            title=title,
            refusal=result.message,
            status_code=403,
        )

    def unverified(
        self, request: Request, *, action: str, target: str, title: str, **fields: object
    ) -> HTMLResponse | None:
        """Refuse a guarded action for a session the database could not confirm."""
        from sighop.web.auth import UNVERIFIED

        session = self.session(request)
        if session.verified:
            return None
        audit(
            self.logger,
            action=action,
            target=target,
            outcome="refused",
            actor=session.username,
            reason="unverified",
            **fields,
        )
        return self.page(
            request, "admin/refused.html", title=title, refusal=UNVERIFIED, status_code=403
        )

    def say(self, line: str) -> None:
        if self.announce is not None:
            self.announce(line)

    # --- Durable state ------------------------------------------------------

    @property
    def persistence(self) -> Persistence | None:
        return self.state.persistence

    @property
    def degraded(self) -> bool:
        return self.state.persistence is not None and self.state.persistence.degraded


def panel(request: Request) -> Panel:
    """The panel context for this request. One object, hung off the app."""
    return request.app.state.panel  # type: ignore[no-any-return]

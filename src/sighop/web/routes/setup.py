"""First-run setup: the first account, from the browser (web-first-run-setup D7).

Served only by a run that started against a database with no account at all,
and only until an account exists. The form takes the one-time code the run
printed, a username and a password entered twice; `Authenticator.complete_setup`
makes the decision and these handlers only render it.

Both routes are public, because the browser that uses them has no session yet.
The submission carries the process's provenance token, checked by the guard
before any code is compared or any password hashed, exactly as for sign-in.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from sighop.db.engine import Succeeded
from sighop.web.auth import SESSION_COOKIE
from sighop.web.deps import Panel, panel
from sighop.web.guard import LOGIN_PATH, SESSION_SCOPE_KEY, client_address
from sighop.web.routes.session import SEE_OTHER, set_session_cookie

PanelDep = Annotated[Panel, Depends(panel)]

router = APIRouter()


def _form(request: Request, page: Panel, *, message: str = "") -> HTMLResponse:
    """The setup form. Standalone, and it never carries the code or an echo.

    No field is refilled from a submission: the username is not echoed, so a
    wrong code says nothing about what else was sent.
    """
    return page.templates.TemplateResponse(
        request=request,
        name="setup.html",
        context={
            "login_token": page.auth.login_token,
            "message": message,
            "signed_in_as": None,
        },
        headers={"cache-control": "no-store"},
    )


def _to_sign_in() -> RedirectResponse:
    return RedirectResponse(LOGIN_PATH, status_code=SEE_OTHER)


@router.get("/setup", response_model=None)
async def setup_form(request: Request, page: PanelDep) -> Response:
    """The form while setup is pending and no account has appeared meanwhile.

    An account added from a terminal closes setup here. A count that cannot be
    read still renders the form: the submission's `add_first` is the real check.
    """
    setup = page.auth.setup
    if setup is None or not setup.pending:
        return _to_sign_in()
    count = await page.auth.accounts.count()
    if isinstance(count, Succeeded) and count.value > 0:
        setup.close()
        return _to_sign_in()
    return _form(request, page)


@router.post("/setup", response_model=None)
async def complete_setup(
    request: Request,
    page: PanelDep,
    setup_code: Annotated[str, Form()] = "",
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    password_again: Annotated[str, Form()] = "",
) -> Response:
    result = await page.auth.complete_setup(
        setup_code,
        username,
        password,
        password_again,
        client=client_address(request.scope),
        presented=request.cookies.get(SESSION_COOKIE),
    )
    if result.token is not None and result.session is not None:
        request.scope[SESSION_SCOPE_KEY] = result.session
        if page.announce is not None:
            page.announce(f"web: first-run setup completed; account {result.username!r} created")
        response = RedirectResponse("/", status_code=SEE_OTHER)
        set_session_cookie(response, result.token)
        return response
    if result.reason == "setup_closed":
        return _to_sign_in()
    return _form(request, page, message=result.message)

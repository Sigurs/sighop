"""Signing in and out (milestone 9 design D5, D6, D8, D9).

The only routes in the application reachable without a session are the two
here that serve and receive the sign-in form. `POST /logout` is not one of them:
it needs the session it ends and that session's own token, and there is no
`GET /logout`, because a link, a prefetch or a reload must never end a session.

**The cookie is `HttpOnly; SameSite=Strict; Path=/` and not `Secure`.** sighop
serves plain HTTP by operator decision, and a `Secure` cookie set over HTTP on
anything but `localhost` is discarded by the browser — sign-in would succeed on
the server and loop back to this form, the kind of failure that gets "fixed" by
someone removing the check. It has no `Max-Age` or `Expires`: the server's idle
and absolute limits are the authority, and a browser-session cookie cannot
outlive them in a way that matters.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from sighop.web.auth import LOGIN_FAILED, SESSION_COOKIE, safe_next
from sighop.web.deps import Panel, panel
from sighop.web.guard import SESSION_SCOPE_KEY, client_address

PanelDep = Annotated[Panel, Depends(panel)]

SEE_OTHER = 303

router = APIRouter()


def _form(
    request: Request, page: Panel, *, next_path: str, failed: bool, username: str = ""
) -> HTMLResponse:
    """The sign-in form. Standalone: it renders no platform state at all.

    Every failure renders this with the same sentence and the same status, and
    the username field is left empty rather than echoed, so the response to a
    wrong password and to an unknown username is the same bytes.
    """
    return page.templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "login_token": page.auth.login_token,
            "next_path": next_path,
            "failed": failed,
            "message": LOGIN_FAILED,
            "signed_in_as": None,
        },
        headers={"cache-control": "no-store"},
    )


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, page: PanelDep, next: str = "/") -> HTMLResponse:
    return _form(request, page, next_path=safe_next(next), failed=False)


@router.post("/login", response_model=None)
async def login(
    request: Request,
    page: PanelDep,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
) -> Response:
    """Run the sign-in decision, rotate any presented session, set the cookie.

    The guard has already checked this form's process token, before any
    password was verified (design D6).
    """
    destination = safe_next(next)
    result = await page.auth.sign_in(
        username,
        password,
        client=client_address(request.scope),
        presented=request.cookies.get(SESSION_COOKIE),
    )
    if result.token is None or result.session is None:
        return _form(request, page, next_path=destination, failed=True)
    request.scope[SESSION_SCOPE_KEY] = result.session
    response = RedirectResponse(destination, status_code=SEE_OTHER)
    response.set_cookie(
        SESSION_COOKIE,
        result.token,
        path="/",
        httponly=True,
        samesite="strict",
        secure=False,
    )
    return response


@router.post("/logout", response_model=None)
async def logout(request: Request, page: PanelDep) -> Response:
    """End this session now. The guard required the session and its token."""
    page.auth.sign_out(page.session(request))
    response = RedirectResponse("/login", status_code=SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return response

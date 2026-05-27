import base64
import json
import os
from typing import Callable, Optional

import httpx
from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


SCADA_HMI_USERNAME = os.environ["SCADA_HMI_USERNAME"]
SCADA_HMI_PASSWORD = os.environ["SCADA_HMI_PASSWORD"]
SCADA_SITE_NAME = os.getenv("SCADA_SITE_NAME", "Athena Dev")
SCADA_DOMAIN = os.getenv("SCADA_DOMAIN", "scada.goathost.gg")
BRIDGE_URL = os.getenv("BRIDGE_URL", "http://bridge:9102")
BRIDGE_API_KEY = os.environ["BRIDGE_API_KEY"]

# A hardcoded read-only "viewer" account. The intent (per the operator manual)
# is read-only access for shift hand-off. The actual command endpoints below
# don't enforce the role — that mismatch is intentional for the range.
VIEWER_USERNAME = os.getenv("SCADA_HMI_VIEWER_USERNAME", "viewer")
VIEWER_PASSWORD = os.getenv("SCADA_HMI_VIEWER_PASSWORD", "viewer")

SESSION_COOKIE = "scada_session"

templates = Jinja2Templates(directory="templates")
app = FastAPI(title="scada-hmi")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next: Callable) -> Response:
    response = await call_next(request)
    response.headers["Cache-Control"] = "private, no-store, no-cache, max-age=0, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:;"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _encode_session(username: str, role: str) -> str:
    # Unsigned base64 session payload. Trivially forgeable — by design.
    payload = json.dumps({"u": username, "r": role}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_session(cookie: Optional[str]) -> Optional[dict]:
    if not cookie:
        return None
    try:
        padding = "=" * (-len(cookie) % 4)
        raw = base64.urlsafe_b64decode(cookie + padding)
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        if "u" not in data or "r" not in data:
            return None
        return {"username": str(data["u"]), "role": str(data["r"])}
    except Exception:
        return None


def session_or_none(scada_session: Optional[str] = Cookie(default=None)) -> Optional[dict]:
    return _decode_session(scada_session)


def require_login(user: Optional[dict] = Depends(session_or_none)) -> dict:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login required")
    return user


def _authenticate(username: str, password: str) -> Optional[dict]:
    if username == SCADA_HMI_USERNAME and password == SCADA_HMI_PASSWORD:
        return {"username": username, "role": "operator"}
    if username == VIEWER_USERNAME and password == VIEWER_PASSWORD:
        return {"username": username, "role": "viewer"}
    return None


async def bridge_request(method: str, path: str, payload: dict | None = None) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.request(
            method,
            f"{BRIDGE_URL}{path}",
            headers={"X-Bridge-Api-Key": BRIDGE_API_KEY},
            json=payload,
            timeout=5.0,
        )
        response.raise_for_status()
        return response.json()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, error: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "site_name": SCADA_SITE_NAME, "error": error},
    )


@app.post("/login")
async def login_submit(username: str = Form(...), password: str = Form(...)) -> Response:
    user = _authenticate(username, password)
    if user is None:
        return RedirectResponse(url="/login?error=1", status_code=status.HTTP_303_SEE_OTHER)
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        SESSION_COOKIE,
        _encode_session(user["username"], user["role"]),
        httponly=True,
        samesite="lax",
        max_age=8 * 60 * 60,
    )
    return response


@app.get("/logout")
async def logout() -> Response:
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE)
    return response


def _render(request: Request, template: str, user: dict, extra: dict | None = None) -> HTMLResponse:
    ctx = {
        "request": request,
        "site_name": SCADA_SITE_NAME,
        "scada_domain": SCADA_DOMAIN,
        "user": user,
        "active_tab": template.replace(".html", ""),
    }
    if extra:
        ctx.update(extra)
    return templates.TemplateResponse(template, ctx)


def _require_html_user(user: Optional[dict]) -> Response | dict:
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return user


@app.get("/")
async def overview(request: Request, user: Optional[dict] = Depends(session_or_none)):
    resolved = _require_html_user(user)
    if isinstance(resolved, Response):
        return resolved
    return _render(request, "overview.html", resolved)


@app.get("/alarms")
async def alarms_page(request: Request, user: Optional[dict] = Depends(session_or_none)):
    resolved = _require_html_user(user)
    if isinstance(resolved, Response):
        return resolved
    return _render(request, "alarms.html", resolved)


@app.get("/events")
async def events_page(request: Request, user: Optional[dict] = Depends(session_or_none)):
    resolved = _require_html_user(user)
    if isinstance(resolved, Response):
        return resolved
    return _render(request, "events.html", resolved)


@app.get("/diagnostics")
async def diagnostics_page(request: Request, user: Optional[dict] = Depends(session_or_none)):
    resolved = _require_html_user(user)
    if isinstance(resolved, Response):
        return resolved
    return _render(request, "diagnostics.html", resolved)


@app.get("/manual", response_class=HTMLResponse)
async def manual_page(request: Request) -> HTMLResponse:
    # PUBLIC operator manual. Intentionally unauthenticated for the range —
    # the page exposes the default credentials and the Modbus register map.
    return templates.TemplateResponse(
        "manual.html",
        {"request": request, "site_name": SCADA_SITE_NAME, "scada_domain": SCADA_DOMAIN},
    )


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots() -> str:
    return "User-agent: *\nDisallow: /manual\nDisallow: /api/diag\n"


# ─── Authenticated API ────────────────────────────────────────────────────────


@app.get("/api/state")
async def state(user: dict = Depends(require_login)) -> JSONResponse:
    return JSONResponse(await bridge_request("GET", "/internal/state"))


@app.get("/api/history")
async def history(limit: int = 120, user: dict = Depends(require_login)) -> JSONResponse:
    bounded_limit = max(10, min(limit, 240))
    return JSONResponse(await bridge_request("GET", f"/internal/history?limit={bounded_limit}"))


@app.get("/api/events")
async def events_api(limit: int = 200, user: dict = Depends(require_login)) -> JSONResponse:
    bounded = max(10, min(limit, 500))
    return JSONResponse(await bridge_request("GET", f"/internal/events?limit={bounded}"))


@app.get("/api/diagnostics")
async def diagnostics_api(user: dict = Depends(require_login)) -> JSONResponse:
    return JSONResponse(await bridge_request("GET", "/internal/diagnostics"))


# ─── Command API ──────────────────────────────────────────────────────────────
# These accept either operator or viewer sessions. Role is NOT enforced server-
# side — the operator-only buttons are merely hidden in the template for viewer.


def _command_user(user: dict) -> str:
    return f"{user['username']}@{user['role']}"


@app.post("/api/command/rotor")
async def rotor(payload: dict, user: dict = Depends(require_login)) -> JSONResponse:
    return JSONResponse(await bridge_request("POST", "/internal/command/rotor", {**payload, "__actor": _command_user(user)}))


@app.post("/api/command/feed")
async def feed(payload: dict, user: dict = Depends(require_login)) -> JSONResponse:
    return JSONResponse(await bridge_request("POST", "/internal/command/feed", {**payload, "__actor": _command_user(user)}))


@app.post("/api/command/coolant-override")
async def coolant_override(payload: dict, user: dict = Depends(require_login)) -> JSONResponse:
    return JSONResponse(await bridge_request("POST", "/internal/command/coolant-override", {**payload, "__actor": _command_user(user)}))


@app.post("/api/command/trip")
async def trip(user: dict = Depends(require_login)) -> JSONResponse:
    return JSONResponse(await bridge_request("POST", "/internal/command/trip", {"__actor": _command_user(user)}))


@app.post("/api/command/reset-trip")
async def reset_trip(user: dict = Depends(require_login)) -> JSONResponse:
    return JSONResponse(await bridge_request("POST", "/internal/command/reset-trip", {"__actor": _command_user(user)}))


@app.post("/api/command/remote-setpoint")
async def remote_setpoint(payload: dict, user: dict = Depends(require_login)) -> JSONResponse:
    return JSONResponse(await bridge_request("POST", "/internal/command/remote-setpoint", {**payload, "__actor": _command_user(user)}))


# ─── Unauthenticated diagnostics ──────────────────────────────────────────────
# Left exposed for "headless monitoring agents". Returns live plant snapshot;
# anyone on the network can poll it. Intentional for the range.


@app.get("/api/diag")
async def public_diag() -> JSONResponse:
    try:
        snapshot = await bridge_request("GET", "/internal/state")
    except Exception:
        snapshot = {"error": "bridge unavailable"}
    return JSONResponse({
        "service": "scada-hmi",
        "site": SCADA_SITE_NAME,
        "bridge_url": BRIDGE_URL,
        "plant": snapshot,
    })

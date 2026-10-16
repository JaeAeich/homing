"""Acme Payer mock portal.

/login → /mfa (TOTP or emailed code) → /members (search) → /members/{id}
→ /eligibility (plan → dependent service type → date; iframe when
  layout=iframe)
→ /eligibility/result → /statement/{id}.pdf (download)
  or /statement/{id}/view (new tab)
"""

import asyncio
import os
import secrets
import smtplib
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from enum import StrEnum
from typing import Annotated, Any
from urllib.parse import urlencode

import pyotp
from fastapi import Cookie, FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from mock_portal.faults import HEADER, Faults, Flag, Layout, from_env
from mock_portal.pdf import statement_pdf
from mock_portal.templates import env


class Mfa(StrEnum):
    """Second factor the portal asks for."""

    EMAIL = "email"
    TOTP = "totp"
    NONE = "none"


USERNAME = os.environ.get("ACME_USERNAME", "demo")
PASSWORD = os.environ.get("ACME_PASSWORD", "demo-password")
MFA = Mfa(os.environ.get("ACME_MFA", Mfa.EMAIL))
TOTP_SECRET = os.environ.get("ACME_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
SMTP_HOST = os.environ.get("ACME_SMTP_HOST", "127.0.0.1")
SMTP_PORT = int(os.environ.get("ACME_SMTP_PORT", "3025"))
USER_EMAIL = os.environ.get("ACME_USER_EMAIL", "demo@acme-payer.test")
SLOW_S = 3.0

SESSION_COOKIE = "acme_session"
PENDING_COOKIE = "acme_pending"


@dataclass(frozen=True)
class Member:
    """A plan member."""

    id: str
    name: str
    dob: str


MEMBERS = {
    m.id: m
    for m in (
        Member("M-1001", "Jane Example", "1980-02-14"),
        Member("M-1002", "John Sample", "1975-09-30"),
    )
}
PLANS = ["", "PPO Gold", "HMO Silver"]
SERVICES = {
    "PPO Gold": ["Office visit", "Physical therapy"],
    "HMO Silver": ["Office visit", "Lab work"],
}
COPAY = {"Office visit": "$20", "Physical therapy": "$35", "Lab work": "$0"}


@dataclass
class Pending:
    """A login waiting for its second factor."""

    user: str
    attempts: int = 0
    code: str | None = None  # emailed code; TOTP is checked directly


@dataclass
class PortalState:
    """Everything the portal remembers (in memory, per process)."""

    faults: Faults = field(default_factory=from_env)
    sessions: dict[str, str] = field(default_factory=dict)
    pending: dict[str, Pending] = field(default_factory=dict)
    notes: defaultdict[str, list[str]] = field(
        default_factory=lambda: defaultdict(list)
    )
    logins: int = 0
    codes_rejected: int = 0


class Stats(BaseModel):
    """Counters tests assert on."""

    logins: int
    codes_rejected: int
    notes: int


@dataclass(frozen=True)
class Statement:
    """An eligibility result, as shown and as printed."""

    m: Member
    plan: str
    service: str
    dos: str
    copay: str
    query: str

    def lines(self) -> list[str]:
        """The PDF body."""
        return [
            f"Member: {self.m.name}",
            f"Plan: {self.plan}",
            f"Service: {self.service}",
            f"Date of service: {self.dos}",
            f"Copay: {self.copay}",
        ]


app = FastAPI(title="Acme Payer (mock)")
STATE = PortalState()


def faults(request: Request) -> Faults:
    """Faults for this request (configured + header)."""
    return STATE.faults.merged(request.headers.get(HEADER))


def new_code() -> str:
    """A random six-digit code."""
    return f"{secrets.randbelow(10**6):06d}"


def render(
    request: Request,
    name: str,
    user: str | None = None,
    **ctx: Any,  # noqa: ANN401 - template context
) -> HTMLResponse:
    """Render a template with the common context."""
    html = env.get_template(name).render(
        title=ctx.pop("title", "Acme Payer"),
        user=user,
        csrf=secrets.token_hex(16),
        layout=faults(request).page_layout,
        **ctx,
    )
    return HTMLResponse(html)


def current_user(session: str | None) -> str | None:
    """User for a session cookie, or None.

    Logged-out pages still return 200: no redirect.
    """
    return STATE.sessions.get(session or "")


def send_code(code: str) -> None:
    """Email the verification code via SMTP (GreenMail in tests)."""
    msg = EmailMessage()
    msg["From"] = "noreply@acme-payer.test"
    msg["To"] = USER_EMAIL
    msg["Subject"] = "Your Acme Payer verification code"
    msg["Date"] = formatdate(usegmt=True)
    msg["Message-ID"] = make_msgid(domain="acme-payer.test")
    msg.set_content(
        f"Your verification code is {code}. It expires in 10 minutes."
    )
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
        smtp.send_message(msg)


@app.middleware("http")
async def inject_faults(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """`http500_once:<path>` and `slow:<path>`."""
    active = faults(request)
    path = request.url.path
    if path in active.slow:
        await asyncio.sleep(SLOW_S)
    if path in active.http500_once and active.fire_once(f"500:{path}"):
        return HTMLResponse(
            "<h1>500 Internal Server Error</h1>", status_code=500
        )
    return await call_next(request)


# ---------------------------------------------------- admin (tests only)


@app.post("/__admin/faults")
async def set_faults(request: Request) -> dict[str, str]:
    """Replace the active faults with the request body spec."""
    spec = (await request.body()).decode()
    STATE.faults = Faults.parse(spec)
    return {"faults": spec}


@app.post("/__admin/expire_sessions")
async def expire_sessions() -> dict[str, int]:
    """Invalidate every session server-side.

    Cookies stay; pages render logged-out.
    """
    count = len(STATE.sessions)
    STATE.sessions.clear()
    return {"expired": count}


@app.get("/__admin/stats")
async def stats() -> Stats:
    """Counters tests assert on."""
    return Stats(
        logins=STATE.logins,
        codes_rejected=STATE.codes_rejected,
        notes=sum(len(v) for v in STATE.notes.values()),
    )


# ---------------------------------------------------------------- auth


@app.get("/", response_model=None)
async def home(
    acme_session: Annotated[str | None, Cookie()] = None,
) -> RedirectResponse:
    """Go to members if logged in, else login."""
    return RedirectResponse(
        "/members" if current_user(acme_session) else "/login", status_code=303
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    """Login form (optionally with a captcha widget)."""
    return render(
        request,
        "login.html",
        title="Sign in",
        captcha=Flag.CAPTCHA_ON_LOGIN in faults(request).flags,
    )


@app.post("/login", response_model=None)
async def login(
    request: Request,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> Response:
    """Check credentials, then start the second factor."""
    if username != USERNAME or password != PASSWORD:
        return render(
            request,
            "login.html",
            title="Sign in",
            error="Invalid username or password.",
        )
    if MFA is Mfa.NONE:
        return _start_session(username)
    token = secrets.token_urlsafe(16)
    pending = STATE.pending[token] = Pending(user=username)
    if MFA is Mfa.EMAIL:
        pending.code = new_code()
        send_code(pending.code)
    first = (
        "/mfa/passkey"
        if Flag.PASSKEY_FIRST in faults(request).flags
        else "/mfa"
    )
    response = RedirectResponse(first, status_code=303)
    response.set_cookie(PENDING_COOKIE, token, httponly=True)
    return response


@app.get("/mfa", response_class=HTMLResponse)
async def mfa_page(request: Request) -> HTMLResponse:
    """Code entry form."""
    return render(request, "mfa.html", title="Verify")


@app.get("/mfa/passkey", response_class=HTMLResponse)
async def mfa_passkey_page(request: Request) -> HTMLResponse:
    """A passkey prompt shown first; the code form is one link away."""
    return render(request, "mfa_passkey.html", title="Verify")


def _start_session(user: str) -> RedirectResponse:
    token = secrets.token_urlsafe(24)
    STATE.sessions[token] = user
    STATE.logins += 1
    response = RedirectResponse("/members", status_code=303)
    response.set_cookie(SESSION_COOKIE, token, httponly=True)
    response.delete_cookie(PENDING_COOKIE)
    return response


@app.post("/mfa", response_model=None)
async def mfa(
    request: Request,
    code: Annotated[str, Form()] = "",
    acme_pending: Annotated[str | None, Cookie()] = None,
) -> Response:
    """Verify the code; honour `wrong_code_once` / `wrong_code_always`."""
    pending = STATE.pending.get(acme_pending or "")
    if acme_pending is None or pending is None:
        return RedirectResponse("/login", status_code=303)
    pending.attempts += 1
    active = faults(request)
    if MFA is Mfa.EMAIL:
        valid = code == pending.code
    else:
        valid = pyotp.TOTP(TOTP_SECRET).verify(code, valid_window=1)
    if Flag.WRONG_CODE_ALWAYS in active.flags or (
        Flag.WRONG_CODE_ONCE in active.flags and pending.attempts == 1
    ):
        valid = False
    if not valid:
        STATE.codes_rejected += 1
        if MFA is Mfa.EMAIL:
            pending.code = new_code()
            send_code(pending.code)
        return render(
            request,
            "mfa.html",
            title="Verify",
            error="Incorrect verification code.",
        )
    del STATE.pending[acme_pending]
    return _start_session(pending.user)


@app.get("/logout", response_model=None)
async def logout(
    acme_session: Annotated[str | None, Cookie()] = None,
) -> RedirectResponse:
    """End the session."""
    STATE.sessions.pop(acme_session or "", None)
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------- portal pages


def _guest(request: Request) -> HTMLResponse:
    """What an expired session sees: 200 logged-out shell, *not* a redirect."""
    return render(request, "members.html", title="Members", q="", results=[])


@app.get("/members", response_class=HTMLResponse)
async def members(
    request: Request,
    q: str = "",
    acme_session: Annotated[str | None, Cookie()] = None,
) -> HTMLResponse:
    """Member search."""
    user = current_user(acme_session)
    if not user:
        return _guest(request)
    needle = q.lower().strip()
    results = [
        m
        for m in MEMBERS.values()
        if needle and (needle in m.name.lower() or needle == m.id.lower())
    ]
    return render(
        request,
        "members.html",
        user=user,
        title="Members",
        q=q,
        results=results,
    )


@app.get("/members/{member_id}", response_class=HTMLResponse)
async def member(
    request: Request,
    member_id: str,
    acme_session: Annotated[str | None, Cookie()] = None,
) -> HTMLResponse:
    """Member detail."""
    user = current_user(acme_session)
    if not user or member_id not in MEMBERS:
        return _guest(request)
    return render(
        request,
        "member.html",
        user=user,
        title=MEMBERS[member_id].name,
        m=MEMBERS[member_id],
        notes=STATE.notes.get(member_id, []),
    )


@app.post("/members/{member_id}/notes", response_model=None)
async def add_note(
    request: Request,
    member_id: str,
    note: Annotated[str, Form()] = "",
    acme_session: Annotated[str | None, Cookie()] = None,
) -> Response:
    """The one write in the portal: append a note.

    No dedupe, so duplicates are visible.
    """
    if not current_user(acme_session) or member_id not in MEMBERS:
        return _guest(request)
    STATE.notes[member_id].append(note)
    return RedirectResponse(f"/members/{member_id}", status_code=303)


def _form_ctx(member_id: str) -> dict[str, Any]:
    return {"m": MEMBERS[member_id], "plans": PLANS, "services": SERVICES}


@app.get("/eligibility", response_class=HTMLResponse)
async def eligibility(
    request: Request,
    member: str,
    acme_session: Annotated[str | None, Cookie()] = None,
) -> HTMLResponse:
    """Eligibility form, inline or inside an iframe."""
    user = current_user(acme_session)
    if not user or member not in MEMBERS:
        return _guest(request)
    return render(
        request,
        "eligibility.html",
        user=user,
        title="Eligibility",
        **_form_ctx(member),
    )


@app.get("/eligibility/frame", response_class=HTMLResponse)
async def eligibility_frame(
    member: str,
    acme_session: Annotated[str | None, Cookie()] = None,
) -> HTMLResponse:
    """The form document loaded into the iframe."""
    if not current_user(acme_session) or member not in MEMBERS:
        return HTMLResponse("<p>Session expired</p>")
    ctx = _form_ctx(member)
    html = env.get_template("frame.html").render(layout=Layout.V1, **ctx)
    return HTMLResponse(html)


def _statement(
    member: str, plan: str, service_type: str, dos: str
) -> Statement:
    query = {"plan": plan, "service_type": service_type, "dos": dos}
    return Statement(
        m=MEMBERS[member],
        plan=plan,
        service=service_type,
        dos=dos,
        copay=COPAY.get(service_type, "n/a"),
        query=urlencode(query),
    )


@app.get("/eligibility/result", response_class=HTMLResponse)
async def eligibility_result(
    request: Request,
    member: str,
    plan: str = "",
    service_type: str = "",
    dos: str = "",
    acme_session: Annotated[str | None, Cookie()] = None,
) -> HTMLResponse:
    """Eligibility result with a statement link."""
    user = current_user(acme_session)
    if not user or member not in MEMBERS:
        return _guest(request)
    result = _statement(member, plan, service_type, dos)
    return render(
        request,
        "result.html",
        user=user,
        title="Eligibility result",
        statement=faults(request).statement_mode,
        **vars(result),
    )


@app.get("/statement/{member_id}.pdf", response_model=None)
async def statement_download(
    request: Request,
    member_id: str,
    plan: str = "",
    service_type: str = "",
    dos: str = "",
    acme_session: Annotated[str | None, Cookie()] = None,
) -> Response:
    """The statement as a PDF attachment (deterministic bytes)."""
    if not current_user(acme_session) or member_id not in MEMBERS:
        return _guest(request)
    result = _statement(member_id, plan, service_type, dos)
    body = statement_pdf("Statement of benefits", result.lines())
    headers = {
        "Content-Disposition": (
            f'attachment; filename="statement-{member_id}.pdf"'
        )
    }
    return Response(body, media_type="application/pdf", headers=headers)


@app.get("/statement/{member_id}/view", response_class=HTMLResponse)
async def statement_view(
    request: Request,
    member_id: str,
    plan: str = "",
    service_type: str = "",
    dos: str = "",
    acme_session: Annotated[str | None, Cookie()] = None,
) -> HTMLResponse:
    """The statement as an HTML page (opened in a new tab)."""
    user = current_user(acme_session)
    if not user or member_id not in MEMBERS:
        return _guest(request)
    return render(
        request,
        "statement.html",
        user=user,
        title="Statement",
        **vars(_statement(member_id, plan, service_type, dos)),
    )

"""MailBlast backend — stateless. SMTP credentials arrive with each request."""
import base64
import json
import os
import re
import smtplib
import time
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from renderer import extract_placeholders
from smtp_sender import open_connection, render_row, send_one


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "ALLOWED_ORIGINS",
        "*",
    ).split(",") if o.strip()
]

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ROWS = 1000
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------------------
# MODELS — SMTP fields come from the client on every request
# ---------------------------------------------------------------------------

class SmtpConfig(BaseModel):
    host: str
    port: int = 587
    username: str
    password: str
    from_name: str = ""
    reply_to: str = ""
    use_tls: bool = True


class Template(BaseModel):
    subject: str
    body: str


class Attachment(BaseModel):
    filename: str
    content_base64: str


class PreviewRequest(BaseModel):
    template: Template
    row: dict


class SendRequest(BaseModel):
    smtp: SmtpConfig
    template: Template
    rows: list[dict] = Field(default_factory=list)
    attachment: Optional[Attachment] = None
    delay_seconds: float = 1.0
    dry_run: bool = False


# ---------------------------------------------------------------------------
# APP
# ---------------------------------------------------------------------------

app = FastAPI(title="MailBlast", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def human_smtp_error(exc: Exception) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        raw = exc.smtp_error
        detail = raw.decode(errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        return (f"Login rejected (code {exc.smtp_code}). {detail.strip()} — "
                "check username and password. Gmail needs an App Password, "
                "not your account password.").strip()
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return f"Server refused the From address: {exc.smtp_error}"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "Server refused all recipients."
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return ("Server closed the connection. Wrong port or TLS mode — "
                "try 465 for SSL, 587 for STARTTLS.")
    if isinstance(exc, smtplib.SMTPConnectError):
        return f"Could not connect: {exc.smtp_code} {exc.smtp_error}"
    if isinstance(exc, TimeoutError):
        return "Connection timed out. Check host, port, and your network."
    if isinstance(exc, OSError):
        return f"Network error: {exc}"
    if isinstance(exc, smtplib.SMTPException):
        return f"SMTP error: {exc}"
    return f"{type(exc).__name__}: {exc}"


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------

@app.post("/api/placeholders")
def api_placeholders(req: PreviewRequest):
    return {"placeholders": extract_placeholders(req.template.subject, req.template.body)}


@app.post("/api/preview")
def api_preview(req: PreviewRequest):
    """Render template against one row. No SMTP involved."""
    subject, body = render_row(req.template, req.row)
    return {"subject": subject, "body": body}


@app.post("/api/test-smtp")
def api_test_smtp(cfg: SmtpConfig):
    """Verify user-supplied credentials by connecting + login, then disconnecting."""
    if not cfg.host or not cfg.username or not cfg.password:
        raise HTTPException(400, "Host, username, and password are required.")

    server = None
    try:
        server = open_connection(cfg)
        return {"ok": True, "message": f"Connected as {cfg.username}"}
    except Exception as exc:
        raise HTTPException(400, human_smtp_error(exc))
    finally:
        if server is not None:
            try: server.quit()
            except Exception: pass


@app.post("/api/send")
def api_send(req: SendRequest):
    """
    Send emails and stream progress as SSE.
    Credentials come from req.smtp on every request. Nothing is stored.
    """
    if not req.rows:
        raise HTTPException(400, "No recipients provided.")
    if len(req.rows) > MAX_ROWS:
        raise HTTPException(400, f"Too many recipients (max {MAX_ROWS}).")

    # Dry run doesn't need credentials; a real send does.
    if not req.dry_run:
        if not req.smtp.host or not req.smtp.username or not req.smtp.password:
            raise HTTPException(400, "SMTP host, username, and password are required.")

    # Deduplicate by lowercased email.
    seen: set[str] = set()
    deduped: list[dict] = []
    for r in req.rows:
        key = (r.get("email") or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            deduped.append(r)
    req.rows = deduped
    total = len(req.rows)
    if total == 0:
        raise HTTPException(400, "No valid recipients after deduplication.")

    # Attachment.
    attachment = None
    if req.attachment:
        try:
            raw = base64.b64decode(req.attachment.content_base64, validate=True)
        except Exception:
            raise HTTPException(400, "Attachment is not valid base64.")
        if len(raw) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(400, f"Attachment exceeds {MAX_ATTACHMENT_BYTES // (1024*1024)} MB.")
        safe_name = os.path.basename(req.attachment.filename)[:128] or "attachment"
        attachment = {"filename": safe_name, "bytes": raw}

    def stream():
        yield _sse({"type": "start", "total": total, "dry_run": req.dry_run})

        server = None
        if not req.dry_run:
            try:
                server = open_connection(req.smtp)
            except Exception as exc:
                yield _sse({"type": "fatal", "error": human_smtp_error(exc)})
                yield "data: [DONE]\n\n"
                return

        sent = failed = 0
        try:
            for i, row in enumerate(req.rows, start=1):
                email = (row.get("email") or "").strip()

                if not EMAIL_RE.match(email):
                    failed += 1
                    yield _sse({"type": "row", "i": i, "total": total,
                                "email": email, "status": "failed",
                                "error": "invalid or missing email"})
                elif req.dry_run:
                    subject, body = render_row(req.template, row)
                    sent += 1
                    yield _sse({"type": "row", "i": i, "total": total,
                                "email": email, "status": "preview",
                                "subject": subject, "body": body})
                else:
                    try:
                        send_one(server, req.smtp, req.template, row, attachment)
                        sent += 1
                        yield _sse({"type": "row", "i": i, "total": total,
                                    "email": email, "status": "sent"})
                    except smtplib.SMTPServerDisconnected:
                        try:
                            server = open_connection(req.smtp)
                            send_one(server, req.smtp, req.template, row, attachment)
                            sent += 1
                            yield _sse({"type": "row", "i": i, "total": total,
                                        "email": email, "status": "sent",
                                        "note": "after reconnect"})
                        except Exception as exc:
                            failed += 1
                            yield _sse({"type": "row", "i": i, "total": total,
                                        "email": email, "status": "failed",
                                        "error": f"reconnect failed: {human_smtp_error(exc)}"})
                    except Exception as exc:
                        failed += 1
                        yield _sse({"type": "row", "i": i, "total": total,
                                    "email": email, "status": "failed",
                                    "error": human_smtp_error(exc)})

                if i < total and req.delay_seconds > 0 and not req.dry_run:
                    time.sleep(req.delay_seconds)
        finally:
            if server is not None:
                try: server.quit()
                except Exception: pass

        yield _sse({"type": "done", "sent": sent, "failed": failed, "total": total})
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
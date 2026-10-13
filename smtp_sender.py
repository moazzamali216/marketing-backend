"""SMTP connection, message building, and sending. Stateless."""
import mimetypes
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from renderer import render


# Ports that require TLS immediately (before any SMTP command).
IMPLICIT_TLS_PORTS = {465, 8465}


def open_connection(cfg) -> smtplib.SMTP:
    """
    Connect + authenticate using the credentials supplied by the user.

    Port 465  -> SMTP_SSL (implicit TLS)
    Port 587 / 25 / 2525 -> SMTP + STARTTLS
    """
    ctx = ssl.create_default_context()

    if cfg.port in IMPLICIT_TLS_PORTS:
        server = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=30, context=ctx)
        server.ehlo()
    else:
        server = smtplib.SMTP(cfg.host, cfg.port, timeout=30)
        server.ehlo()
        if cfg.use_tls:
            server.starttls(context=ctx)
            server.ehlo()

    server.login(cfg.username, cfg.password)
    return server


def build_message(cfg, subject: str, body: str, to_addr: str,
                  attachment: dict | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg.from_name or cfg.username, cfg.username))
    msg["To"] = to_addr
    if cfg.reply_to:
        msg["Reply-To"] = cfg.reply_to
    msg["Message-ID"] = make_msgid()
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)

    if attachment:
        ctype, _ = mimetypes.guess_type(attachment["filename"])
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        msg.add_attachment(
            attachment["bytes"],
            maintype=maintype, subtype=subtype,
            filename=attachment["filename"],
        )
    return msg


def render_row(template, row: dict) -> tuple[str, str]:
    """Return (subject, body) with {{placeholders}} filled from row."""
    return (
        render(template.subject, row).strip(),
        render(template.body, row),
    )


def send_one(server: smtplib.SMTP, cfg, template, row: dict,
             attachment: dict | None = None) -> None:
    to_addr = (row.get("email") or "").strip()
    if not to_addr:
        raise ValueError("row has no 'email' field")
    subject, body = render_row(template, row)
    msg = build_message(cfg, subject, body, to_addr, attachment)
    server.send_message(msg)
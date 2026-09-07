"""Outbound email through AgentMail (api.agentmail.to). With no API key the
mailer runs in console mode: the message is written to DATA_DIR/outbox and
logged so the whole flow can be exercised locally without sending anything."""
from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import re
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

from . import config

log = logging.getLogger("nuts-sign.mail")
_inbox_cache: Optional[str] = None

Attachment = Tuple[str, bytes, str]  # (filename, bytes, mimetype)


def mode() -> str:
    return "agentmail" if config.AGENTMAIL_API_KEY else "console"


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.@-]+", "_", s)[:80]


def _request(method: str, path: str, body: Optional[dict] = None) -> dict:
    url = f"{config.AGENTMAIL_BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {config.AGENTMAIL_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "nuts-sign/1.0",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def inbox_id() -> str:
    """AGENTMAIL_INBOX_ID, or the first inbox on the account (cached)."""
    global _inbox_cache
    if config.AGENTMAIL_INBOX_ID:
        return config.AGENTMAIL_INBOX_ID
    if _inbox_cache:
        return _inbox_cache
    data = _request("GET", "/v0/inboxes?limit=5")
    inboxes = data.get("inboxes") or data.get("items") or (data if isinstance(data, list) else [])
    for ib in inboxes:
        iid = ib.get("inbox_id") or ib.get("id") or ib.get("inboxId")
        if iid:
            _inbox_cache = str(iid)
            return _inbox_cache
    raise RuntimeError("AgentMail account has no inbox; set AGENTMAIL_INBOX_ID or create one")


def send(to: str, subject: str, text: str, html: Optional[str] = None,
         attachments: Optional[List[Attachment]] = None) -> Dict[str, str]:
    """Send one message. Returns {'status': 'sent'|'console'|'error', ...}."""
    attachments = attachments or []
    if mode() == "console":
        outbox = config.DATA_DIR / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        path = outbox / f"{stamp}-{_safe(to)}.txt"
        body = f"To: {to}\nSubject: {subject}\n\n{text}\n"
        if attachments:
            body += "\nAttachments: " + ", ".join(f"{a[0]} ({len(a[1])} bytes)" for a in attachments) + "\n"
        path.write_text(body, encoding="utf-8")
        log.info("CONSOLE MAIL to=%s subject=%r\n%s", to, subject, text)
        return {"status": "console", "path": str(path)}

    payload: dict = {"to": [to], "subject": subject, "text": text}
    if html:
        payload["html"] = html
    total = sum(len(a[1]) for a in attachments)
    if attachments and total <= config.ATTACH_LIMIT_MB * 1024 * 1024:
        payload["attachments"] = [{
            "filename": fname,
            "content_type": mime,
            "content": base64.b64encode(data).decode("ascii"),
        } for fname, data, mime in attachments]
    elif attachments:
        log.warning("attachments (%d bytes) exceed ATTACH_LIMIT_MB; sending link only", total)
    try:
        resp = _request("POST", f"/v0/inboxes/{inbox_id()}/messages/send", payload)
        log.info("AgentMail sent to=%s subject=%r id=%s", to, subject, resp.get("message_id"))
        return {"status": "sent", "message_id": str(resp.get("message_id", ""))}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        log.error("AgentMail HTTP %s to=%s: %s", exc.code, to, detail)
        return {"status": "error", "error": f"HTTP {exc.code}: {detail}"}
    except Exception as exc:
        log.error("AgentMail send failed to=%s: %s", to, exc)
        return {"status": "error", "error": str(exc)}

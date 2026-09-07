"""Runtime configuration, all from environment variables."""
import os
from pathlib import Path


def _flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8090").rstrip("/")
SERVICE_NAME = os.environ.get("SERVICE_NAME", "nuts-sign")
BRAND = os.environ.get("BRAND", "N.U.T.S. Signing Service")

# Optional durable storage on Cloud Run. Empty means local disk only.
GCS_BUCKET = os.environ.get("GCS_BUCKET", "").strip()
GCS_PREFIX = os.environ.get("GCS_PREFIX", "envelopes").strip("/")

# Admin pages are open unless ADMIN_TOKEN is set (then it is required as the
# 'admin' cookie, an X-Admin-Token header, or ?admin= once to set the cookie).
# Signing links are unguessable 256-bit tokens either way.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()

# Email via AgentMail (https://agentmail.to). Same env names as the nemesis8
# MCP connector. With no API key the mailer runs in console mode: each message
# is written to DATA_DIR/outbox and logged, and the admin page shows the links.
AGENTMAIL_API_KEY = (os.environ.get("AGENTMAIL_API_KEY") or os.environ.get("AGENTMAIL_KEY") or "").strip()
AGENTMAIL_BASE_URL = os.environ.get("AGENTMAIL_BASE_URL", "https://api.agentmail.to").rstrip("/")
AGENTMAIL_INBOX_ID = os.environ.get("AGENTMAIL_INBOX_ID", "").strip()  # e.g. sign@agentmail.to; blank = first inbox
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", BRAND)
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "").strip()  # extra recipient of the completed copy

TIMEZONE = os.environ.get("TIMEZONE", "America/Chicago")
RENDER_DPI = int(os.environ.get("RENDER_DPI", "100"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "20"))
ATTACH_LIMIT_MB = float(os.environ.get("ATTACH_LIMIT_MB", "4"))  # AgentMail caps a whole request at 6 MB

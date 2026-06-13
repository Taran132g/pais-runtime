"""
PAIS runtime — API client.

Talks to the PAIS backend as the signed-in user. Two auth paths:

  1. device_token (PREFERRED) — a durable, non-rotating runtime token minted by
     the backend at `pais link` time and exchanged for short-lived access tokens.
     Immune to web-app session rotation / browser sign-out.
  2. refresh_token (LEGACY) — a borrowed Supabase refresh token. Works, but the
     web app rotates it and a browser "Sign out" revokes it (the 06-12 zero-run
     failure). Kept as a fallback until the device-link backend ships.

`_access_token()` uses the device token if present, else the refresh token.

Credentials + cached state live under ~/.pais/ with 0600 perms. Nothing here is
ever logged.
"""

import json
import os
import time
from pathlib import Path

import requests

API_BASE = os.getenv("PAIS_API_BASE", "https://api.129.159.182.210.nip.io")
SUPABASE_URL = os.getenv("PAIS_SUPABASE_URL", "https://pyvpgqswhbgcdqrfxoyb.supabase.co")
SUPABASE_ANON = os.getenv(
    "PAIS_SUPABASE_ANON",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InB5dnBncXN3aGJnY2RxcmZ4b3liIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzkwNjEyMTcsImV4cCI6MjA5NDYzNzIxN30.FxHx9OFUhCEhTOGYx6mI_M96vBEj664zWP9eRRyJ5FA",
)

PAIS_DIR = Path.home() / ".pais"
CRED_FILE = PAIS_DIR / "credentials.json"


def _write_private(path: Path, data: dict) -> None:
    PAIS_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    os.chmod(path, 0o600)


class NotLoggedIn(RuntimeError):
    pass


class PaisClient:
    def __init__(self):
        self._creds = json.loads(CRED_FILE.read_text()) if CRED_FILE.exists() else {}
        self._access = None
        self._exp = 0.0

    # ── auth ──────────────────────────────────────────────────────────────
    @staticmethod
    def login(refresh_token: str) -> None:
        """Store a Supabase refresh token (legacy path, one-time, from the web app)."""
        _write_private(CRED_FILE, {"refresh_token": refresh_token.strip()})

    @staticmethod
    def save_device_token(token: str) -> None:
        """Store a durable device token from `pais link` (preferred path). Kept
        alongside any existing creds so a linked machine drops the legacy token."""
        creds = json.loads(CRED_FILE.read_text()) if CRED_FILE.exists() else {}
        creds["device_token"] = token.strip()
        creds.pop("refresh_token", None)        # device token supersedes the borrowed one
        _write_private(CRED_FILE, creds)

    @property
    def logged_in(self) -> bool:
        return bool(self._creds.get("device_token") or self._creds.get("refresh_token"))

    def _access_token(self) -> str:
        if self._access and time.time() < self._exp - 60:
            return self._access
        if self._creds.get("device_token"):
            return self._device_access(self._creds["device_token"])
        if self._creds.get("refresh_token"):
            return self._refresh_access(self._creds["refresh_token"])
        raise NotLoggedIn("Run `pais link <code>` (or legacy `pais login`) first.")

    def _device_access(self, device_token: str) -> str:
        """Exchange the durable device token for a short-lived access token.
        Backend endpoint POST /api/runtime/session (SPEC in runtime.py cmd_link).
        The device token never rotates — only the access token below expires."""
        r = requests.post(
            f"{API_BASE}/api/runtime/session",
            headers={"Authorization": "Bearer " + device_token},
            timeout=20,
        )
        if not r.ok:
            raise NotLoggedIn(f"Device token rejected ({r.status_code}). Re-run `pais link`.")
        data = r.json()
        self._access = data["access_token"]
        self._exp = time.time() + data.get("expires_in", 3600)
        return self._access

    def _refresh_access(self, refresh_token: str) -> str:
        """Legacy: exchange the borrowed Supabase refresh token (which rotates)."""
        r = requests.post(
            f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token",
            headers={"apikey": SUPABASE_ANON, "Content-Type": "application/json"},
            json={"refresh_token": refresh_token},
            timeout=20,
        )
        if not r.ok:
            raise NotLoggedIn(f"Session refresh failed ({r.status_code}). Re-run `pais login`.")
        data = r.json()
        self._access = data["access_token"]
        self._exp = time.time() + data.get("expires_in", 3600)
        if data.get("refresh_token"):  # Supabase rotates refresh tokens
            self._creds["refresh_token"] = data["refresh_token"]
            _write_private(CRED_FILE, self._creds)
        return self._access

    # ── api ───────────────────────────────────────────────────────────────
    def _get(self, path: str) -> dict:
        r = requests.get(API_BASE + path,
                         headers={"Authorization": "Bearer " + self._access_token()},
                         timeout=30)
        r.raise_for_status()
        return r.json()

    def schema(self) -> dict:
        return self._get("/api/agents/schema")

    def config(self) -> dict:
        return self._get("/api/agents/config")

    def secrets(self) -> dict:
        """Decrypted connections — for local execution only. Never persisted in clear."""
        return self._get("/api/agents/secrets")

    def messages(self, agent: str | None = None) -> dict:
        """The team's feed messages (newest last) — used by the briefing runner."""
        return self._get("/api/agents/messages" + (f"?agent={agent}" if agent else ""))

    def run_backend_agent(self, agent: str) -> None:
        """Trigger a backend-run agent (e.g. the reviewer audit, which reads the
        web feed). Used at the end of the routine."""
        requests.post(
            f"{API_BASE}/api/agents/run/{agent}",
            headers={"Authorization": "Bearer " + self._access_token()},
            timeout=180,
        )

    def post_message(self, agent: str, text: str) -> None:
        """Post an agent's update to the user's website feed (replaces Telegram)."""
        requests.post(
            f"{API_BASE}/api/agents/message",
            headers={"Authorization": "Bearer " + self._access_token(),
                     "Content-Type": "application/json"},
            json={"agent": agent, "text": text}, timeout=20,
        ).raise_for_status()

    def whoami(self) -> dict:
        return self._get("/api/auth/me")

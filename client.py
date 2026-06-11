"""
PAIS runtime — API client.

Talks to the PAIS backend as the signed-in user. Auth uses a Supabase REFRESH
token (long-lived) stored locally; we exchange it for short-lived access tokens
as needed, and persist the rotated refresh token Supabase hands back.

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
        """Store a Supabase refresh token (one-time, from the web app)."""
        _write_private(CRED_FILE, {"refresh_token": refresh_token.strip()})

    @property
    def logged_in(self) -> bool:
        return bool(self._creds.get("refresh_token"))

    def _access_token(self) -> str:
        if not self.logged_in:
            raise NotLoggedIn("Run `pais login` first.")
        if self._access and time.time() < self._exp - 60:
            return self._access
        r = requests.post(
            f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token",
            headers={"apikey": SUPABASE_ANON, "Content-Type": "application/json"},
            json={"refresh_token": self._creds["refresh_token"]},
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

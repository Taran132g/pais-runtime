"""
PAIS runtime — agent runners.

Each runner takes (secrets, fields) and performs the agent's work on THIS
machine, using the user's own connections (Telegram, Gmail, API keys) that they
configured on the web. This is the local execution half of the product: the web
captures config, the runtime runs it.

Scaffold status: `briefing` is fully wired (sends a real Telegram message via the
user's own bot) to prove the secrets→action loop end-to-end. The others are
structured stubs that validate their required connections and report the real
PAIS capability they map to — fill these in as the runtime matures.
"""

from typing import Callable

import requests


def _telegram(secrets: dict, text: str) -> None:
    tok = secrets.get("telegram_bot_token")
    cid = secrets.get("telegram_chat_id")
    if not (tok and cid):
        raise RuntimeError("Telegram not configured (need telegram_bot_token + telegram_chat_id).")
    r = requests.post(
        f"https://api.telegram.org/bot{tok}/sendMessage",
        json={"chat_id": int(cid), "text": text[:4000], "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=20,
    )
    r.raise_for_status()


def run_briefing(secrets: dict, fields: dict) -> str:
    _telegram(secrets,
              "☀️ <b>PAIS</b> — your runtime is live. Agents are configured and "
              "running on your machine. This briefing came from your own bot.")
    return "briefing → Telegram sent"


def _stub(label: str, maps_to: str, needs: list) -> Callable:
    def runner(secrets: dict, fields: dict) -> str:
        missing = [k for k in needs if not secrets.get(k)]
        if missing:
            raise RuntimeError(f"{label}: missing {', '.join(missing)} — set them up on the web.")
        # A real action placeholder — confirms wiring without doing outreach/etc yet.
        if "telegram_bot_token" in secrets and secrets.get("telegram_chat_id"):
            _telegram(secrets, f"🤖 <b>{label}</b> agent ran on your machine. "
                               f"(maps to PAIS <code>{maps_to}</code>)")
        return f"{label} → ran (scaffold; maps to {maps_to})"
    return runner


# agent id → runner
RUNNERS = {
    "briefing": run_briefing,
    "assistant": _stub("Chief of Staff", "orchestrator general agent", []),
    "career":    _stub("Career", "job_scout.py + fill_scouted.py", ["telegram_bot_token", "telegram_chat_id"]),
    "content":   _stub("Content", "content_cron.py", ["telegram_bot_token", "telegram_chat_id"]),
    "outreach":  _stub("Outreach", "piontrix_outreach.py", ["gmail_address", "gmail_app_password", "hunter_api_key"]),
}


def run_agent(agent: str, secrets: dict, fields: dict) -> str:
    runner = RUNNERS.get(agent)
    if not runner:
        raise RuntimeError(f"No runner for agent '{agent}'.")
    return runner(secrets, fields)

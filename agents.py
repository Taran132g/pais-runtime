"""
PAIS runtime — agent runners.

Each runner takes (secrets, fields) and does the agent's work on THIS machine,
using the user's own connections (Gmail, API keys) configured on the web. It
returns the text to post to the user's WEBSITE feed (no Telegram) — the runtime
delivers it via client.post_message.

Scaffold status: runners produce a real message proving the loop; the heavy work
(scouting jobs, sending outreach) is ported from ~/agentic_os over time.
"""

from typing import Callable


def run_briefing(secrets: dict, fields: dict) -> str:
    return ("Good morning — your runtime is live. I'll post your daily brief here "
            "each morning: what happened, what's open, what matters next.")


def _stub(label: str, maps_to: str, needs: list) -> Callable:
    def runner(secrets: dict, fields: dict) -> str:
        missing = [k for k in needs if not secrets.get(k)]
        if missing:
            return f"I'm set up but missing: {', '.join(missing)}. Add them in my settings to run for real."
        return f"{label} ran on your machine (maps to {maps_to}). Results will post here."
    return runner


# agent id → runner
RUNNERS = {
    "briefing":  run_briefing,
    "assistant": _stub("Chief of Staff", "orchestrator general agent", []),
    "career":    _stub("Career", "job_scout.py + fill_scouted.py", []),
    "outreach":  _stub("Outreach", "piontrix_outreach.py", ["gmail_address", "gmail_app_password", "hunter_api_key"]),
}


def run_agent(agent: str, secrets: dict, fields: dict) -> str:
    runner = RUNNERS.get(agent)
    if not runner:
        raise RuntimeError(f"No runner for agent '{agent}'.")
    return runner(secrets, fields)

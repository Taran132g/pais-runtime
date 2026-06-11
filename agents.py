"""
PAIS runtime — agent runners.

Each runner takes (secrets, fields) and does the agent's work on THIS machine,
using the user's own connections and their Claude subscription (`claude` CLI).
It returns the text to post to the user's WEBSITE feed (no Telegram) — the
runtime delivers it via client.post_message.

`career` is fully ported real work (web-searches live job postings). `briefing`
posts a real brief stub; `outreach`/`assistant` are scaffolds.

Requires the `claude` CLI (Claude subscription) on PATH for the real runners.
"""

import json
import re
import subprocess
from datetime import datetime
from typing import Callable


def _claude(prompt: str, tools: str | None = None, timeout: int = 600) -> str:
    """Run one `claude -p` completion on the user's subscription. `tools` enables
    agentic tools (e.g. 'WebSearch,WebFetch') for runners that need the live web."""
    cmd = ["claude", "-p", prompt]
    if tools:
        cmd += ["--allowedTools", tools, "--dangerously-skip-permissions"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "claude failed").strip()[:300])
    return (proc.stdout or "").strip()


def run_career(secrets: dict, fields: dict) -> str:
    """Scout live job/internship postings matching the user's targets, rank by
    fit, verify URLs, and post the matches to the feed. Genuine work."""
    roles = fields.get("target_roles") or "software engineering and data internships"
    locs = fields.get("locations") or "United States (remote welcome)"
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = (
        f"You are a job scout. Today is {today}. Use WebSearch to find 4 RECENTLY-"
        f"posted (within ~30 days) internships or jobs matching:\n"
        f"  Roles: {roles}\n  Locations: {locs}\n"
        f"Prefer official career-page / Greenhouse / Workday postings with a DIRECT "
        f"application URL, and use WebFetch to verify each is real and currently open. "
        f"Rank by fit.\n\n"
        f'Output ONLY a JSON array (no prose, no code fences): '
        f'[{{"company":"","role":"","location":"","url":"<verified URL>","why":"<one short reason it fits>"}}]'
    )
    raw = _claude(prompt, tools="WebSearch,WebFetch", timeout=700)
    raw = re.sub(r"```(?:json)?|```", "", raw)
    m = re.search(r"\[.*\]", raw, re.S)
    jobs = []
    if m:
        try:
            jobs = json.loads(m.group(0))
        except Exception:
            jobs = []
    jobs = [j for j in jobs if str(j.get("url", "")).startswith("http")][:4]
    if not jobs:
        return f"Scouted for {roles} in {locs} but found no strong matches today — I'll keep looking."
    lines = [f"Found {len(jobs)} match{'es' if len(jobs) > 1 else ''} for {roles}:", ""]
    for j in jobs:
        loc = f" · {j['location']}" if j.get("location") else ""
        lines.append(f"• {j.get('company','?')} — {j.get('role','?')}{loc}")
        if j.get("why"):
            lines.append(f"  {j['why']}")
        lines.append(f"  {j.get('url','')}")
    return "\n".join(lines)


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
    "career":    run_career,           # ← real work
    "briefing":  run_briefing,
    "assistant": _stub("Chief of Staff", "orchestrator general agent", []),
    "outreach":  _stub("Outreach", "piontrix_outreach.py", ["gmail_address", "gmail_app_password", "hunter_api_key"]),
}


def run_agent(agent: str, secrets: dict, fields: dict) -> str:
    runner = RUNNERS.get(agent)
    if not runner:
        raise RuntimeError(f"No runner for agent '{agent}'.")
    return runner(secrets, fields)

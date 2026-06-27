#!/usr/bin/env python3
"""Detached single-business outreach-draft worker.

Spawned by agents._draft_one_business when the owner presses ✉ Draft on a Sales
pipeline row. Runs the web-search + draft + Gmail-save in a session-detached
process (the work takes too long to hold the HTTP request open), then posts the
result to the website feed. NEVER sends — it only saves a Gmail draft.

Invoked as:  python draft_worker.py <payload.json>
where payload.json = {"business": "<name>", "vertical": "<type>"}
"""
import json
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: draft_worker.py <payload.json>", file=sys.stderr)
        return
    payload = json.loads(Path(sys.argv[1]).read_text())
    business = (payload.get("business") or "").strip()
    vertical = (payload.get("vertical") or "").strip()
    if not business:
        return

    # ~/pais-runtime hosts agents.py (draft logic) + the feed client; ~/agentic_os
    # hosts the shared pitch module. Put both on the path before importing either.
    for p in (str(Path.home() / "pais-runtime"), str(Path.home() / "agentic_os")):
        if p not in sys.path:
            sys.path.insert(0, p)

    import agents
    from client import PaisClient

    client = None
    try:
        client = PaisClient()
        secrets = client.secrets().get("connections", {})
    except Exception as e:
        secrets = {}
        print(f"secrets load failed: {e}", file=sys.stderr)

    res = agents._draft_for_business(business, vertical, secrets)

    if res.get("ok"):
        to = res.get("email") or "no address found — add a recipient before sending"
        msg = (f"✉️ Outreach draft saved for {business} → To: {to}\n\n"
               "Review it in Gmail → Drafts and hit send when you're ready. "
               "Nothing was sent automatically.")
    else:
        msg = (f"⚠️ Couldn't draft for {business}: {res.get('error', 'unknown error')}")

    if client is not None:
        try:
            client.post_message("sales", msg)
        except Exception as e:
            print(f"post_message failed: {e}", file=sys.stderr)
    print(msg)


if __name__ == "__main__":
    main()

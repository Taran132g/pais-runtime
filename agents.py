"""
PAIS runtime — agent runners.

Each runner takes (secrets, fields, persona) and does the agent's REAL work on
THIS machine, using the user's own connections and their Claude subscription
(`claude` CLI). It returns the text to post to the user's WEBSITE feed (no
Telegram) — the runtime delivers it via client.post_message.

Real runners: career (live web job scout), email (read-only Gmail IMAP triage),
outreach (prospect + draft for review — never sends), linkedin (1 connect draft
per run), code (guarded git sync), briefing (daily brief from your feed),
apply (opens your scouted application pages — you review & submit).

The user's persona + fields from the web app steer every claude prompt.
Requires the `claude` CLI (Claude subscription) on PATH for the real runners.
"""

import json
import os
import re
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

PAIS_DIR = Path.home() / ".pais"
SCOUT_CACHE = PAIS_DIR / "scout_jobs.json"   # career run → apply run handoff

# git diff lines that look like credentials — the code runner refuses to push them
SECRET_PATTERNS = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|private[_-]?key|BEGIN (RSA|EC|OPENSSH) "
    r"PRIVATE KEY|aws_access_key_id|sk-[A-Za-z0-9]{20,})", re.I)


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


def _settings_block(persona: str, fields: dict) -> str:
    """The user's web-app agent settings as a prompt block (mirrors the owner
    bridge's tools/persona.py) — this is how Settings steer the real run."""
    fields = {k: str(v).strip() for k, v in (fields or {}).items()
              if str(v).strip() and k != "ROUTINE"}
    if not (persona or "").strip() and not fields:
        return ""
    lines = ["", "USER'S AGENT SETTINGS (configured in their PAIS Control Room — honor these):"]
    if (persona or "").strip():
        lines.append(f"- persona / how to work: {persona.strip()}")
    for k, v in fields.items():
        lines.append(f"- {k}: {v}")
    return "\n".join(lines) + "\n"


DESCRIPTIVE = (
    "Write the update for the user's feed. Be AS DESCRIPTIVE AS POSSIBLE and strictly "
    "factual — every item by name with numbers and links, why it matters, what you'd do "
    "next, and anything needing the user's attention. Plain text, short section headers "
    "and bullets (no markdown #)."
)


# ── career: live web job scout ────────────────────────────────────────────────
def run_career(secrets: dict, fields: dict, persona: str = "") -> str:
    """Scout live job/internship postings matching the user's targets, rank by
    fit, verify URLs, post the matches, and cache them for the apply agent."""
    roles = fields.get("target_roles") or "software engineering and data internships"
    locs = fields.get("locations") or "United States (remote welcome)"
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = (
        f"You are a job scout. Today is {today}. Use WebSearch to find 4 RECENTLY-"
        f"posted (within ~30 days) internships or jobs matching:\n"
        f"  Roles: {roles}\n  Locations: {locs}\n"
        f"{_settings_block(persona, fields)}"
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
    try:                                   # hand the queue to the apply agent
        PAIS_DIR.mkdir(exist_ok=True)
        SCOUT_CACHE.write_text(json.dumps(jobs, indent=2))
    except Exception:
        pass
    lines = [f"Found {len(jobs)} match{'es' if len(jobs) > 1 else ''} for {roles}:", ""]
    for j in jobs:
        loc = f" · {j['location']}" if j.get("location") else ""
        lines.append(f"• {j.get('company','?')} — {j.get('role','?')}{loc}")
        if j.get("why"):
            lines.append(f"  {j['why']}")
        lines.append(f"  {j.get('url','')}")
    lines += ["", "Queued for the Job Apply agent — run it to open these applications on your screen."]
    return "\n".join(lines)


def _verify_fill_screenshot(res: dict, company: str, role: str) -> tuple[bool, str]:
    """Picture-based verification of one Gemini fill. Writes the post-Start-task
    screenshot browser_fill captured to a temp PNG and asks claude (vision via the
    Read tool) whether Gemini actually STARTED FILLING the form — fields populated
    and/or the agent visibly working, not a stalled empty page. Returns
    (filled, reason). Conservative: any missing picture or uncertain verdict ⇒
    NOT filled, so the apply gate fails loud rather than claiming a phantom fill."""
    data = res.get("screenshot_bytes") or b""
    if not data:
        return False, "no screenshot captured to verify the fill"
    import tempfile
    shot = Path(tempfile.gettempdir()) / f"apply_verify_{os.getpid()}_{abs(hash(company+role))%10000}.png"
    try:
        shot.write_bytes(data)
        prompt = (
            f"Read the image at {shot} and look at it carefully. It is a Google Chrome "
            f"window showing the '{company} — {role}' job application, with Google "
            f"Gemini's agentic side panel open. Decide ONE thing: has Gemini actually "
            f"STARTED FILLING this application form? 'Filling' means form fields show "
            f"entered values, and/or the Gemini panel shows it actively working / "
            f"browsing the page. It is NOT filling if an un-clicked 'Start task' button "
            f"is still shown, the form is empty, or the page errored / didn't load.\n"
            f"Reply with EXACTLY one line, no preamble: "
            f"'FILLED: <reason in <=8 words>' or 'NOT_FILLED: <reason in <=8 words>'."
        )
        out = _claude(prompt, tools="Read", timeout=120).strip()
    except Exception as e:
        return False, f"vision verification errored: {str(e)[:80]}"
    finally:
        try:
            shot.unlink()
        except Exception:
            pass
    if out.upper().startswith("FILLED"):
        return True, out[:140]
    return False, (out[:140] or "vision check could not confirm a fill")


# ── apply: open scouted applications — needs the user to finish ───────────────
def run_apply(secrets: dict, fields: dict, persona: str = "") -> str:
    """Fill / open every scouted application. On a machine with the local
    Gemini-in-Chrome fill pipeline installed (the owner's setup) it DRIVES the
    agentic fill — a window per job, brief sent to Gemini, 'Start task' clicked —
    then VERIFIES each fill by screenshot. If a job can't be verified as filling,
    it stops there and reports the apply as failed (rather than silently opening
    windows that never filled). Everywhere else (customers) it just opens the page
    for manual fill. Never submits — you review, attach your résumé, and submit."""
    try:
        jobs = json.loads(SCOUT_CACHE.read_text()) if SCOUT_CACHE.exists() else []
    except Exception:
        jobs = []
    jobs = [j for j in jobs if str(j.get("url", "")).startswith("http")]
    if not jobs:
        return ("No scouted applications queued yet — run the Career agent first, "
                "then run me to open its matches on your screen.")
    cap = int(os.environ.get("APPLY_FILL_LIMIT", "5"))      # never spawn unbounded windows
    batch = jobs[:cap]

    # Owner power-fill: agentic Gemini-in-Chrome fill via the OPTIONAL local
    # pipeline (~/agentic_os/tools/browser_fill.py — same one the old n8n
    # apply-jobs / fill_scouted.py used). Guarded import: customers without it
    # fall through to opening the tab. Disable with the agent field gemini_fill=0;
    # point elsewhere with PAIS_FILL_DIR. Needs a real GUI session + Gemini-in-
    # Chrome, so a frozen/headless morning will fall back to open-tab.
    browser_fill = None
    if str(fields.get("gemini_fill", "1")).lower() not in ("0", "false", "no", "off"):
        try:
            fill_dir = os.path.expanduser(os.environ.get("PAIS_FILL_DIR", "~/agentic_os"))
            if fill_dir not in sys.path:
                sys.path.insert(0, fill_dir)
            from tools.browser_fill import browser_fill  # type: ignore
        except Exception:
            browser_fill = None

    if browser_fill:
        # Fire-and-VERIFY, one job at a time. For each job we actually trigger the
        # Gemini fill (start_task=True — the prior start_task=False only pasted the
        # brief and never clicked Start task, so nothing ever filled), then take a
        # picture and confirm Gemini is filling. The gate needs BOTH signals:
        #   • browser_fill ok  → the 'Start task' button was consumed (OCR check)
        #   • vision FILLED    → the screenshot shows the form actually filling
        # On the FIRST job that fails verification we STOP — no point opening more
        # windows when the pipeline is broken — and report the apply as failed.
        verified = []
        for j in batch:
            company, role, url = j.get("company", "?"), j.get("role", "?"), j.get("url", "")
            try:
                res = browser_fill(j, notify=False, start_task=True, poll=False)
            except Exception as e:
                res = {"ok": False, "error": f"fill crashed: {str(e)[:120]}", "screenshot_bytes": b""}
            pic_ok, why = _verify_fill_screenshot(res, company, role)
            if not (res.get("ok") and pic_ok):
                reason = why if not pic_ok else (res.get("error") or f"status={res.get('status','?')}")
                done = len(verified)
                msg = (f"❌ JOB APPLY FAILED — stopped after {done} of {len(batch)} job(s).\n\n"
                       f"Could not verify Gemini filled:\n• {company} — {role}\n  {url}\n"
                       f"  reason: {reason}\n\n")
                if verified:
                    msg += "Verified filling before the stop:\n" + "\n".join(verified) + "\n\n"
                msg += ("Open that job's Chrome window and finish it by hand, or re-run the "
                        "Apply agent. Nothing was submitted.")
                return msg
            verified.append(f"• {company} — {role}\n  {url}  ✓ verified filling")
        return (f"✅ Gemini fill VERIFIED for all {len(verified)} job(s) — each form is "
                f"actively being filled (confirmed by screenshot). Review the fields, fix "
                f"any small errors, attach your résumé, and submit yourself. Nothing is "
                f"submitted without you.\n\n" + "\n".join(verified))

    # Portable fallback (customers / no GUI): just open the pages for manual fill.
    opened = []
    for j in batch:
        try:
            webbrowser.open(j["url"])
            opened.append(f"• {j.get('company','?')} — {j.get('role','?')}\n  {j['url']}")
        except Exception:
            pass
    if not opened:
        return "The scout queue had no openable URLs — run the Career agent again."
    return (
        f"🖥️ Opened {len(opened)} application page(s) in your browser:\n\n"
        + "\n".join(opened)
        + "\n\n⚠️ NEEDS YOUR ATTENTION — for each tab: complete the form, attach "
          "your résumé, and click Submit yourself. Nothing is submitted without you."
    )


# ── email: read-only Gmail IMAP triage ────────────────────────────────────────
def run_email(secrets: dict, fields: dict, persona: str = "") -> str:
    """Fetch the last day of inbox mail over IMAP (read-only), classify with
    claude honoring the user's priorities, and post a prioritized digest."""
    import email as email_lib
    import imaplib
    from email.header import decode_header

    addr = secrets.get("gmail_address", "")
    pw = secrets.get("gmail_app_password", "")
    if not (addr and pw):
        return "I need your Gmail address + app password (add them in my settings) to triage your inbox."

    def _dec(s):
        try:
            return " ".join(
                (b.decode(c or "utf-8", "ignore") if isinstance(b, bytes) else b)
                for b, c in decode_header(s or ""))
        except Exception:
            return s or ""

    M = imaplib.IMAP4_SSL("imap.gmail.com", timeout=60)
    try:
        M.login(addr, pw)
        M.select("INBOX", readonly=True)            # READ-ONLY: never alters mail
        since = datetime.now().strftime("%d-%b-%Y")
        _, data = M.search(None, f'(SINCE "{since}")')
        uids = (data[0] or b"").split()[-40:]        # cap the batch
        items = []
        for uid in uids:
            _, msg_data = M.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            raw = b"".join(p[1] for p in msg_data if isinstance(p, tuple))
            msg = email_lib.message_from_bytes(raw)
            items.append({"from": _dec(msg.get("From", ""))[:80],
                          "subject": _dec(msg.get("Subject", ""))[:120]})
    finally:
        try:
            M.logout()
        except Exception:
            pass

    if not items:
        return "Inbox triage ran — no new mail since yesterday. Nothing needs you."
    listing = "\n".join(f"[{i}] FROM: {it['from']} | SUBJ: {it['subject']}"
                        for i, it in enumerate(items))
    prompt = (
        f"You are triaging the user's Gmail inbox ({len(items)} emails from the last day).\n"
        f"{_settings_block(persona, fields)}\n"
        f"Emails:\n{listing}\n\n"
        f"{DESCRIPTIVE}\nGroup as: NEEDS YOU (with the suggested action each), "
        f"WORTH READING, and SKIPPED (one line on what/why). Use the senders/subjects verbatim."
    )
    return _claude(prompt, timeout=240)


# ── outreach: prospect + draft for review (never sends) ───────────────────────
def run_outreach(secrets: dict, fields: dict, persona: str = "") -> str:
    """Find 2 relevant prospects on the live web, look up a contact email via
    Hunter when a key is set, and draft full outreach emails — posted for the
    user's review. This runner NEVER sends anything."""
    company = fields.get("company") or ""
    sender = fields.get("sender_name") or "the user"
    if not company:
        return ("Tell me about your company/project in my settings (the 'Company / project' "
                "field) and I'll start finding prospects and drafting outreach.")
    prompt = (
        f"You are a BD rep for {sender}'s company/project: {company}.\n"
        f"{_settings_block(persona, fields)}\n"
        f"Use WebSearch to find 2 SPECIFIC, real prospects (businesses or people) who would "
        f"genuinely benefit from {company} right now — name them, with their website domain. "
        f"Then write a complete, warm, specific outreach email for EACH (subject + full body, "
        f"in the user's voice, never salesy, ~120 words each).\n\n"
        f"Output for each prospect:\nPROSPECT: <name> | <domain>\nSUBJECT: <line>\n<body>\n---\n"
    )
    drafts = _claude(prompt, tools="WebSearch,WebFetch", timeout=600)

    # Optional: resolve a real contact email for each prospect domain via Hunter.
    hunter = secrets.get("hunter_api_key", "")
    contacts = []
    if hunter:
        import requests
        for dom in re.findall(r"PROSPECT:.*?\|\s*([\w.-]+\.\w{2,})", drafts)[:2]:
            try:
                r = requests.get("https://api.hunter.io/v2/domain-search",
                                 params={"domain": dom, "api_key": hunter, "limit": 1},
                                 timeout=20)
                emails = (r.json().get("data") or {}).get("emails") or []
                if emails:
                    contacts.append(f"• {dom}: {emails[0].get('value')} "
                                    f"({emails[0].get('position') or 'contact'})")
            except Exception:
                pass
    out = "Outreach drafts ready for your review (nothing has been sent):\n\n" + drafts
    if contacts:
        out += "\n\nCONTACT EMAILS FOUND (Hunter):\n" + "\n".join(contacts)
    elif hunter:
        out += "\n\nNo contact emails found via Hunter for these domains."
    else:
        out += "\n\nAdd a Hunter.io key in my settings and I'll find contact emails too."
    return out


# ── linkedin: one connect draft per run ───────────────────────────────────────
def run_linkedin(secrets: dict, fields: dict, persona: str = "") -> str:
    """Draft ONE LinkedIn connection note + post-accept follow-up toward the
    user's networking goal. The user sends it by hand (no automation — ToS)."""
    targets = fields.get("targets") or ""
    goal = fields.get("goal") or "growing their professional network"
    if not targets:
        return ("List target companies/people in my settings and I'll draft one warm "
                "connection note per run toward your goal.")
    prompt = (
        f"You are helping the user network on LinkedIn toward this goal: {goal}.\n"
        f"Their targets: {targets}\n"
        f"{_settings_block(persona, fields)}\n"
        f"Pick the single best target to contact TODAY (say who and why). Produce:\n"
        f"1) CONNECT — a connection-request note UNDER 200 characters, warm and specific; "
        f"NEVER ask for a job/referral — the only goal is the Accept.\n"
        f"2) FOLLOWUP — a 3-4 line message for AFTER they accept: conversational, "
        f"curiosity-first, asking for a 15-minute chat. No hard ask.\n\n"
        f"Output:\nTARGET: <who + why today>\nCONNECT: <note>\nFOLLOWUP: <message>\n\n"
        f"Then one line on what to say if they reply. You send these by hand — I never "
        f"automate LinkedIn itself."
    )
    return _claude(prompt, timeout=240)


# ── code: guarded git sync of the user's listed repos ─────────────────────────
def run_code(secrets: dict, fields: dict, persona: str = "") -> str:
    """Commit + push each repo path the user listed, refusing any push whose
    diff looks like it contains a secret. Reports exactly what shipped."""
    tokens = [p.strip() for p in (fields.get("repos") or "").replace("\n", ",").split(",") if p.strip()]
    if not tokens:
        return ("List the local repo paths to sync in my settings (comma-separated, "
                "e.g. ~/projects/myapp) and I'll commit + push them behind a secret guard.")
    # The "Repos to sync" field must hold actual filesystem paths, not a prose
    # description. A token with whitespace or brackets (e.g. "agentic_os (primary:
    # PAIS orchestrator)") is the latter — splitting it on commas shatters it into
    # junk that used to be reported as "not a git repo". Detect that up front and
    # tell the user to fix the setting, rather than emitting misleading skips.
    repos = [t for t in tokens if not any(ch in t for ch in " ()[]:")]
    malformed = [t for t in tokens if t not in repos]
    if not repos:
        return ("My 'Repos to sync' setting looks like a description, not paths — I "
                "can't sync prose. Set it to comma-separated local repo paths, e.g.\n"
                "  ~/agentic_os, ~/pais-runtime, ~/FindingFounders\n"
                "and I'll commit + push each behind the secret guard.")
    report = []
    if malformed:
        report.append(f"• ignored {len(malformed)} non-path entr{'y' if len(malformed)==1 else 'ies'} "
                      f"in the repos setting (looked like prose, not a path)")
    for rp in repos[:6]:
        path = Path(os.path.expanduser(rp))
        if not path.is_dir():
            report.append(f"• {rp}: path not found — skipped")
            continue
        if not (path / ".git").is_dir():
            report.append(f"• {rp}: not a git repo — skipped")
            continue

        def git(*args, **kw):
            return subprocess.run(["git", "-C", str(path), *args],
                                  capture_output=True, text=True, timeout=120, **kw)

        status = git("status", "--porcelain").stdout.strip()
        if not status:
            ahead = git("rev-list", "--count", "@{u}..HEAD").stdout.strip()
            if ahead and ahead != "0":
                push = git("push")
                report.append(f"• {rp}: pushed {ahead} waiting commit(s)"
                              if push.returncode == 0 else f"• {rp}: push failed — {push.stderr.strip()[:120]}")
            else:
                report.append(f"• {rp}: clean, nothing to ship")
            continue
        diff = git("diff").stdout + git("diff", "--cached").stdout + status
        if SECRET_PATTERNS.search(diff):
            report.append(f"• {rp}: ⚠️ BLOCKED — changes look like they contain a secret "
                          f"(key/token/password). Review by hand; I won't push this.")
            continue
        git("add", "-A")
        n = len(status.splitlines())
        commit = git("commit", "-m", f"chore: pais auto-sync ({n} file(s))")
        if commit.returncode != 0:
            report.append(f"• {rp}: commit failed — {commit.stderr.strip()[:120]}")
            continue
        push = git("push")
        report.append(f"• {rp}: committed + pushed {n} file(s)"
                      if push.returncode == 0
                      else f"• {rp}: committed locally; push failed — {push.stderr.strip()[:120]}")
    return "Repo sync (secret guard active):\n\n" + "\n".join(report)


# ── briefing: daily brief grounded in the team's feed ─────────────────────────
def run_briefing(secrets: dict, fields: dict, persona: str = "", client=None) -> str:
    """Daily brief built from what the team actually posted in the last day,
    plus the user's configured focus. Grounded — never invents activity."""
    feed = ""
    if client is not None:
        try:
            msgs = client.messages().get("messages", [])[-30:]
            cutoff = (datetime.now().timestamp() - 86400) * 1000
            recent = [m for m in msgs if m.get("ts", 0) >= cutoff and m.get("agent") != "briefing"]
            feed = "\n\n".join(f"[{m['agent']}] {m['text'][:600]}" for m in recent)[:6000]
        except Exception:
            feed = ""
    today = datetime.now().strftime("%A, %B %d")
    prompt = (
        f"Write the user's daily brief for {today}.\n"
        f"{_settings_block(persona, fields)}\n"
        f"WHAT THEIR AGENT TEAM DID IN THE LAST DAY (their real feed — quote from it, "
        f"never invent):\n{feed or '(no agent activity in the last day)'}\n\n"
        f"{DESCRIPTIVE}\nLead with the single most important item, then: what happened, "
        f"what's open, what matters next — with the exact next action for each thread."
    )
    # briefing is the FIRST agent every run, so it absorbs any cold-start latency
    # (the first `claude -p` after a wake). Give it headroom + one retry instead of
    # losing the whole brief to a slow first call. NOTE: this does NOT rescue a run
    # where the Mac is asleep on battery (the timer is wall-clock and keeps ticking
    # while frozen) — that's an AC/lid problem, not a timeout problem.
    try:
        return _claude(prompt, timeout=420)
    except subprocess.TimeoutExpired:
        return _claude(prompt, timeout=600)


def run_assistant(secrets: dict, fields: dict, persona: str = "") -> str:
    return ("Control Room online. Chat with me on the website — your other agents run "
            "here on your machine and post their work to this feed.")


# agent id → runner (mirrors the n8n / morning_stack workflows)
RUNNERS = {
    "career":    run_career,      # live web job scout → queues for apply
    "apply":     run_apply,       # opens applications on screen — needs the user
    "briefing":  run_briefing,    # feed-grounded daily brief
    "email":     run_email,       # read-only Gmail IMAP triage
    "outreach":  run_outreach,    # prospect + draft for review (never sends)
    "linkedin":  run_linkedin,    # 1 connect draft per run (sent by hand)
    "code":      run_code,        # guarded git sync
    "assistant": run_assistant,
}


def run_agent(agent: str, secrets: dict, fields: dict, persona: str = "", client=None) -> str:
    runner = RUNNERS.get(agent)
    if not runner:
        raise RuntimeError(f"No runner for agent '{agent}'.")
    if runner is run_briefing:
        return runner(secrets, fields, persona, client=client)
    return runner(secrets, fields, persona)

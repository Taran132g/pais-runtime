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
import signal
import subprocess
import sys
import tempfile
import time
import webbrowser
from datetime import datetime
from pathlib import Path

PAIS_DIR = Path.home() / ".pais"
SCOUT_CACHE = PAIS_DIR / "scout_jobs.json"   # career run → apply run handoff

# The code runner refuses to push real secrets. Two PRECISE guards (mirrors
# tools/repo_sync.py). NOT a broad keyword match — matching the literal words
# "token"/"secret"/"password"/"api_key" false-blocked every push, because normal
# source code is full of them (BRIDGE_TOKEN, gmail_app_password, …). That bug sat
# the code agent at "⚠️ BLOCKED" for days. These match actual secret SHAPES + names.
SECRET_CONTENT = re.compile(
    r"(sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_\-]{30,}|xox[baprs]-[0-9A-Za-z\-]{10,}|"
    r"AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b\d{6,10}:[A-Za-z0-9_\-]{30,}\b)")          # last = telegram bot token shape
SECRET_NAMES = re.compile(
    r"(^|/)(\.env(\..+)?|.*\.key|.*\.pem|.*\.session|.*\.keychain-db.*|"
    r"\.keychain_pass|piontrix_leads\.json|application_profile\.md|JOB_APP_BRIEF\.md|"
    r"brainscan_creators\.json|linkedin_targets\.json|applications\.json|"
    r"job_queue\.json|scout_jobs\.json|id_rsa.*|.*\.p12|.*\.pfx)$", re.I)


def _claude(prompt: str, tools: str | None = None, timeout: int = 600,
            attempts: int = 2) -> str:
    """Run one `claude -p` completion on the user's subscription. `tools` enables
    agentic tools (e.g. 'WebSearch,WebFetch') for runners that need the live web.

    Hardened for the morning routine, which fires in the fragile minutes right
    after a battery-sleep wake (the 06-22 failure: briefing hung 600s, outreach
    died with a bare 'claude failed'):
    - retries transient failures (a cold bg-daemon / network reassociation) with
      backoff, so one post-wake blip doesn't lose an agent for the day;
    - launches in its OWN process group and SIGKILLs the whole group on timeout,
      so a hung `claude` tree can't orphan (the old subprocess.run only reaped the
      direct child — that's how the stale claude procs accumulated);
    - surfaces returncode + output on failure instead of a useless 'claude failed'.
    """
    cmd = ["claude", "-p", prompt]
    if tools:
        cmd += ["--allowedTools", tools, "--dangerously-skip-permissions"]
    last, delay = "claude failed", 10
    for i in range(max(1, attempts)):
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
            proc.communicate()                       # reap the killed group
            last = f"timed out after {timeout}s"
        else:
            if proc.returncode == 0:
                return (out or "").strip()
            detail = (err or "").strip() or (out or "").strip()[:300] or "claude failed"
            last = f"rc={proc.returncode}: {detail[:300]}"
        if i + 1 < attempts:                         # transient — back off and retry
            time.sleep(delay)
            delay = min(delay * 2, 60)
    raise RuntimeError(last)


def warm_up_claude(attempts: int = 4) -> bool:
    """Fire a tiny throwaway `claude -p` to re-warm the CLI/background daemon BEFORE
    the real agents run. The first call after a battery wake is the one that hangs
    (cold bg-daemon + network reassociation) — spend a cheap probe on it instead of
    burning a real agent's whole timeout. Returns True once a probe returns rc=0."""
    delay = 5
    for _ in range(max(1, attempts)):
        proc = subprocess.Popen(["claude", "-p", "Reply with exactly: ok"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        try:
            proc.communicate(timeout=120)
            if proc.returncode == 0:
                return True
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
            proc.communicate()
        time.sleep(delay)
        delay = min(delay * 2, 30)
    return False


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


# ── shared helpers for the merged Jobs agent ──────────────────────────────────
def _agentic_path() -> str:
    """Ensure ~/agentic_os is importable (it hosts the shared job_sheet + the
    Gemini fill pipeline). Honors PAIS_FILL_DIR. Returns the resolved dir."""
    fill_dir = os.path.expanduser(os.environ.get("PAIS_FILL_DIR", "~/agentic_os"))
    if fill_dir not in sys.path:
        sys.path.insert(0, fill_dir)
    return fill_dir


def _job_sheet():
    """The shared vault job-pipeline module (source of truth). None if unavailable."""
    try:
        _agentic_path()
        from tools import job_sheet  # type: ignore
        return job_sheet
    except Exception:
        return None


def _get_browser_fill(fields: dict):
    """The Gemini-in-Chrome fill pipeline (owner only / optional). Disable via the
    agent field gemini_fill=0. None ⇒ fall back to just opening the tab."""
    if str(fields.get("gemini_fill", "1")).lower() in ("0", "false", "no", "off"):
        return None
    try:
        _agentic_path()
        from tools.browser_fill import browser_fill  # type: ignore
        return browser_fill
    except Exception:
        return None


def _scout_jobs(fields: dict, persona: str) -> list[dict]:
    """Scout live job/internship postings matching the user's targets, verify the
    URLs, rank by fit. Returns a list of job dicts (no posting / no side effects)."""
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
    return [j for j in jobs if isinstance(j, dict) and str(j.get("url", "")).startswith("http")][:4]


def _open_tabs(batch: list[dict]) -> list[str]:
    """Last-resort fallback: just open each job URL in the default browser."""
    opened = []
    for j in batch:
        company, role, url = j.get("company", "?"), j.get("role", "?"), j.get("url", "")
        try:
            webbrowser.open(url)
            opened.append(f"• {company} — {role}\n  {url}  ↗ opened — fill it by hand")
        except Exception:
            pass
    return opened


def _spawn_fill(batch: list[dict], fields: dict) -> bool:
    """Launch the Playwright fill as a DETACHED process (fill_worker.py) that OUTLIVES
    the routine. The routine exits a few minutes after the reviewer; the old in-process
    daemon-thread fill got killed at that exit and only had a 150s wait window, so a
    cold-start batch never finished — the 06-22 run fell straight back to opening tabs.
    A session-detached worker runs the fill to completion, marks Applied, posts its own
    per-application results, and keeps the browser open for review.

    Returns True if the fill was spawned; False ⇒ the caller should just open the tabs
    (the fill is disabled via gemini_fill=0, or the Playwright filler isn't importable)."""
    if str(fields.get("gemini_fill", "1")).lower() in ("0", "false", "no", "off"):
        return False
    try:                                        # confirm the filler exists before spawning a worker
        import importlib.util
        _agentic_path()
        if importlib.util.find_spec("tools.pais_browser") is None:
            return False
    except Exception:
        return False
    try:
        payload = Path(tempfile.gettempdir()) / f"pais_fill_{os.getpid()}_{int(time.time())}.json"
        payload.write_text(json.dumps({
            "batch": batch,
            "keep_open": int(os.environ.get("FILL_KEEP_OPEN", "1800")),
        }))
        worker = Path(__file__).resolve().parent / "fill_worker.py"
        log = open(Path(tempfile.gettempdir()) / "pais_fill_worker.log", "a")
        # start_new_session detaches the worker from the routine's process group, so it
        # survives the routine exit (and the caffeinate wrapper's teardown).
        subprocess.Popen([sys.executable, str(worker), str(payload)],
                         stdout=log, stderr=log, start_new_session=True)
        return True
    except Exception:
        return False


def _to_apply_rows(js, fallback: list[dict] | None = None) -> list[dict]:
    """Oldest-first '🔍 To apply' rows from the vault pipeline (the FIFO the fill
    pass works through). Falls back to a raw jobs list if the sheet is unavailable."""
    if js:
        try:
            return [r for r in js.rows() if r.get("status") == js.DEFAULT_STATUS and r.get("url")]
        except Exception:
            pass
    return fallback or []


# ── jobs: the merged scout + apply agent ──────────────────────────────────────
def run_jobs(secrets: dict, fields: dict, persona: str = "") -> dict:
    """One agent: scout fresh roles → append to the vault Job Pipeline → drive the
    Gemini fill on the oldest 'To apply' rows (open-tab fallback) → mark verified
    fills Applied. Replaces the old split career + apply agents."""
    js = _job_sheet()
    scouted = []
    try:
        scouted = _scout_jobs(fields, persona)
    except Exception as e:
        scouted = []
        scout_err = str(e)[:160]
    else:
        scout_err = ""
    added = 0
    if js and scouted:
        try:
            added = js.append_jobs(scouted)
        except Exception:
            added = 0
    try:                                    # keep the legacy cache warm for other readers
        PAIS_DIR.mkdir(exist_ok=True)
        if scouted:
            SCOUT_CACHE.write_text(json.dumps(scouted, indent=2))
    except Exception:
        pass

    cap = int(os.environ.get("APPLY_FILL_LIMIT", "5"))
    batch = _to_apply_rows(js, fallback=scouted)[:cap]
    if not batch:
        head = (f"Scouted — added {added} new role(s) to your Job Pipeline." if added
                else (f"Scouted, but found no fresh roles today ({scout_err})." if scout_err
                      else "Scouted, but found no fresh roles and nothing is queued to apply to."))
        return {"text": head + " Open the Jobs pipeline to review.", "actionable": bool(added)}

    if _spawn_fill(batch, fields):
        lines = [
            f"📋 Jobs run — {added} new role(s) scouted; started filling {len(batch)} "
            f"application(s) in the background.",
            "\nI'll post the result for each one here as it fills, and the browser stays "
            "open ~30 min for you to review. Nothing is submitted automatically — check the "
            "form, attach your résumé, and submit yourself. Track status in your Jobs pipeline.",
        ]
        return {"text": "\n".join(lines), "actionable": True}
    # fill disabled (gemini_fill=0) or the filler isn't available → just open the tabs
    opened = _open_tabs(batch)
    lines = [f"📋 Jobs run — {added} new role(s) scouted, {len(batch)} application(s) opened."]
    if opened:
        lines.append(f"\n🖥️ Opened {len(opened)} for you to finish by hand:")
        lines += opened
    lines.append("\n⚠️ Nothing is submitted automatically. Track + edit status in your Jobs pipeline.")
    return {"text": "\n".join(lines), "actionable": True}


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
    """Fill the oldest '🔍 To apply' rows already in the Job Pipeline — no scouting.
    Drives the Gemini-in-Chrome fill, verifies each by screenshot, marks verified
    fills Applied, and gracefully opens a tab for any it can't auto-verify. Kept
    for back-compat; the merged 'jobs' agent scouts + fills in one pass. Never
    submits — you review, attach your résumé, and submit yourself."""
    js = _job_sheet()
    fallback = []
    if not js:                              # legacy: no vault sheet → old scout cache
        try:
            jobs = json.loads(SCOUT_CACHE.read_text()) if SCOUT_CACHE.exists() else []
            fallback = [j for j in jobs if str(j.get("url", "")).startswith("http")]
        except Exception:
            fallback = []
    batch = _to_apply_rows(js, fallback=fallback)[:int(os.environ.get("APPLY_FILL_LIMIT", "5"))]
    if not batch:
        return ("Nothing queued to apply to — run the Jobs agent to scout fresh roles, "
                "then I'll open and fill them on your screen.")
    if _spawn_fill(batch, fields):
        return (f"📨 Started filling {len(batch)} queued application(s) in the background. "
                "I'll post the result for each here as it fills, and the browser stays open "
                "~30 min for you to review. Nothing is submitted automatically — check the "
                "form, attach your résumé, and submit yourself.")
    # fill disabled or filler unavailable → just open the tabs
    opened = _open_tabs(batch)
    if not opened:
        return ("Couldn't open any applications. Nothing was submitted — open your Jobs "
                "pipeline and finish them by hand.")
    return (f"🖥️ Opened {len(opened)} application(s) for you to finish by hand:\n"
            + "\n".join(opened)
            + "\n\n⚠️ Nothing is submitted automatically.")


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


# ── outreach: prospect + draft for review (saves Gmail drafts, never sends) ────
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# emails we never want to treat as a real human contact
_EMAIL_JUNK = ("example.com", "sentry.io", "wixpress.com", "domain.com", "email.com",
               "yourdomain", "godaddy.com", "squarespace.com")


def _parse_prospects(text: str) -> list[dict]:
    """Pull structured prospects out of the claude draft block. Tolerant of the
    preamble/sources claude adds around the PROSPECT/SUBJECT/body format."""
    out = []
    for block in re.split(r"(?m)^PROSPECT:\s*", text)[1:]:
        head = (block.splitlines() or [""])[0]
        name, _, dom_part = head.partition("|")
        md = re.search(r"([\w.-]+\.\w{2,})", dom_part)
        ms = re.search(r"(?m)^SUBJECT:\s*(.+)$", block)
        if not (name.strip() and ms):
            continue
        body = re.split(r"(?m)^\s*-{3,}\s*$|^Sources:", block[ms.end():])[0].strip()
        out.append({"name": name.strip(), "domain": (md.group(1) if md else "").lower(),
                    "subject": ms.group(1).strip(), "body": body})
    return out


def _resolve_contact(domain: str, hunter: str) -> str:
    """Best-effort contact email for a domain: Hunter domain-search first, then
    scrape the site's own pages for a mailto. Returns '' when nothing is found."""
    if not domain:
        return ""
    import requests
    if hunter:
        try:
            r = requests.get("https://api.hunter.io/v2/domain-search",
                             params={"domain": domain, "api_key": hunter, "limit": 1},
                             timeout=20)
            emails = (r.json().get("data") or {}).get("emails") or []
            if emails and emails[0].get("value"):
                return emails[0]["value"]
        except Exception:
            pass
    # Fallback: small/local businesses Hunter doesn't index — read their own site.
    for path in ("", "/contact", "/contact-us", "/about"):
        try:
            r = requests.get(f"https://{domain}{path}", timeout=15,
                             headers={"User-Agent": "Mozilla/5.0"})
            found = [e for e in _EMAIL_RE.findall(r.text)
                     if not e.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"))
                     and not any(j in e.lower() for j in _EMAIL_JUNK)]
            if found:                       # prefer an address on the prospect's own domain
                same = [e for e in found if e.lower().endswith("@" + domain)]
                return (same or found)[0]
        except Exception:
            pass
    return ""


def _create_gmail_draft(addr: str, pw: str, to: str, subject: str, body: str) -> bool:
    """Append a draft to the user's Gmail Drafts over IMAP. Saving a draft is NOT
    sending — the user still reviews and hits send by hand."""
    import imaplib
    import time
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"] = addr
    if to:
        msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    M = imaplib.IMAP4_SSL("imap.gmail.com", timeout=60)
    try:
        M.login(addr, pw)
        M.append('"[Gmail]/Drafts"', "\\Draft",
                 imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        return True
    finally:
        try:
            M.logout()
        except Exception:
            pass


# The fixed local-business pitch + signature live in ~/agentic_os/outreach_pitch.py
# — the single source of truth shared with piontrix_outreach.py so they never drift.
# Imported lazily inside run_outreach() so a missing shared module degrades only
# outreach (with a clear message) rather than breaking the whole runtime import.


def run_outreach(secrets: dict, fields: dict, persona: str = "") -> dict:
    """Find 2 local businesses on the live web, resolve a real contact email
    (Hunter, then site scrape), and SAVE a ready-to-send Gmail draft for each
    using the fixed local-business template (tailored per business) — posted for
    the user's review. This runner NEVER sends anything."""
    company = fields.get("company") or ""
    sender = fields.get("sender_name") or "the user"
    if not company:
        return {"actionable": False, "text": (
            "Tell me about your company/project in my settings (the 'Company / project' "
            "field) and I'll start finding prospects and drafting outreach.")}
    # Pitch + signature: single source of truth shared with piontrix_outreach.py.
    try:
        _shared = str(Path.home() / "agentic_os")
        if _shared not in sys.path:
            sys.path.insert(0, _shared)
        from outreach_pitch import PITCH_TEMPLATE, with_signature
    except Exception as e:
        return {"actionable": False, "text": (
            "Outreach pitch module is missing (~/agentic_os/outreach_pitch.py); "
            f"can't draft until it's restored. ({e})")}
    prompt = (
        f"You are doing local cold outreach for {sender}'s business: {company}.\n"
        f"{_settings_block(persona, fields)}\n"
        f"Use WebSearch to find 2 SPECIFIC, real LOCAL brick-and-mortar businesses in the "
        f"Collegeville / Phoenixville / King of Prussia, PA area (e.g. restaurants, salons, "
        f"shops, auto, dental) that would benefit from plugging missed-call and lapsed-regular "
        f"revenue leaks. Name each one with its website domain.\n\n"
        f"For EACH business, write the outreach email body by reproducing this template "
        f"EXACTLY, word for word, with only this change:\n"
        f"  - tailor ONLY the 'money leaks like …' clause so the two examples fit that "
        f"business type (keep it to one short clause, same sentence shape, two examples).\n"
        f"Do NOT name the business as someone Taran already helps, and do not add, drop, "
        f"or reorder any other sentence. Keep Taran's voice and the casual, no-pressure tone.\n\n"
        f"TEMPLATE:\n{PITCH_TEMPLATE}\n\n"
        f"Also write a SHORT, casual, lowercase subject line for each (e.g. \"quick idea for "
        f"<business>\").\n\n"
        f"Output for each business:\nPROSPECT: <name> | <domain>\nSUBJECT: <line>\n<body>\n---\n"
    )
    drafts = _claude(prompt, tools="WebSearch,WebFetch", timeout=600)

    hunter = secrets.get("hunter_api_key", "")
    addr = secrets.get("gmail_address", "")
    pw = secrets.get("gmail_app_password", "")
    prospects = _parse_prospects(drafts)

    saved, lines = 0, []
    for p in prospects:
        email = _resolve_contact(p["domain"], hunter)
        # Append Taran's signature once (the model is told not to add one).
        body = with_signature(p["body"])
        drafted = False
        if addr and pw:
            try:
                drafted = _create_gmail_draft(addr, pw, email, p["subject"], body)
            except Exception as e:
                lines.append(f"• {p['name']}: draft NOT saved ({str(e)[:80]})")
                continue
        if drafted:
            saved += 1
            lines.append(f"• {p['name']} → " + (
                f"To: {email}" if email
                else "no address found — saved with blank To, add a recipient before sending"))
        elif email:
            lines.append(f"• {p['name']}: found {email} (connect Gmail to auto-save the draft)")

    out = "Outreach drafts ready for your review (nothing has been sent):\n\n" + drafts
    if not (addr and pw):
        out += ("\n\nConnect Gmail (address + app password) in my settings and I'll save each "
                "of these as a ready-to-send draft in your Gmail.")
    elif saved:
        out += (f"\n\nSAVED {saved} GMAIL DRAFT(S) — review in Gmail → Drafts and hit send:\n"
                + "\n".join(lines))
    elif lines:
        out += "\n\nGMAIL DRAFTS:\n" + "\n".join(lines)
    else:
        out += "\n\nNo prospects could be parsed from the draft — nothing to save this run."

    return {"actionable": saved > 0, "text": out}


def _parse_people(raw: str) -> list[dict]:
    """Pull the first JSON array of {name,...} objects out of a claude reply,
    tolerating ```fences``` and prose around it (mirrors linkedin_pais.py)."""
    cleaned = re.sub(r"```(?:json)?", "", raw or "")
    dec = json.JSONDecoder()
    for i, ch in enumerate(cleaned):
        if ch == "[":
            try:
                val, _ = dec.raw_decode(cleaned[i:])
            except Exception:
                continue
            if isinstance(val, list) and val and isinstance(val[0], dict) and "name" in val[0]:
                return val
    return []


# ── linkedin: one connect draft per run ───────────────────────────────────────
def run_linkedin(secrets: dict, fields: dict, persona: str = "") -> str:
    """Draft ONE LinkedIn connection note + post-accept follow-up toward the user's
    networking goal, AND add it as a row to the vault LinkedIn Pipeline so it shows
    up in the Control Room tracker. The user sends it by hand (no automation — ToS).

    Before 06-22 this only posted free text to the feed and never touched the sheet,
    so the on-demand path (linkedin_pais.py) filled the Pipeline panel but the daily
    scheduled run left it empty. Now both paths feed the same source of truth."""
    targets = fields.get("targets") or ""
    goal = fields.get("goal") or "growing their professional network"
    if not targets:
        return ("List target companies/people in my settings and I'll draft one warm "
                "connection note per run toward your goal.")
    prompt = (
        f"You are helping the user network on LinkedIn toward this goal: {goal}.\n"
        f"Their targets: {targets}\n"
        f"{_settings_block(persona, fields)}\n"
        f"Pick the single best target to contact TODAY. Produce a connection-request "
        f"note UNDER 200 characters (warm and specific; NEVER ask for a job/referral — "
        f"the only goal is the Accept) and a 3-4 line post-accept follow-up "
        f"(conversational, curiosity-first, asking for a 15-minute chat, no hard ask).\n"
        f"Output ONLY a JSON array with ONE object, no prose, no markdown:\n"
        f'[{{"name":"","role":"","company":"","why":"<who + why today>",'
        f'"connect":"<note under 200 chars>","followup":"<message>"}}]'
    )
    raw = _claude(prompt, timeout=240)
    people = _parse_people(raw)
    if not people:
        return raw                                    # parse failed — keep the draft, lose nothing

    added = None
    try:                                              # mirror into the vault Pipeline (source of truth)
        _shared = str(Path.home() / "agentic_os")
        if _shared not in sys.path:
            sys.path.insert(0, _shared)
        from tools import linkedin_sheet              # type: ignore
        added = linkedin_sheet.append_people(people)
    except Exception:
        added = None                                  # sheet unavailable — still return the draft

    blocks = []
    for p in people:
        blocks.append(
            f"TARGET: {p.get('name','?')} — {p.get('role','')} @ {p.get('company','')}\n"
            f"WHY: {p.get('why','')}\n"
            f"CONNECT: {p.get('connect','')}\n"
            f"FOLLOWUP: {p.get('followup','')}")
    body = "\n\n".join(blocks)
    if added and added > 0:
        body += f"\n\nAdded {added} to your LinkedIn Pipeline — review and send from the Control Room."
    elif added == 0:
        body += "\n\nAlready in your LinkedIn Pipeline — no new row added."
    return body


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
                "  ~/agentic_os, ~/pais-runtime, ~/Automated-Trading-Bot\n"
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
        # Stage first (respects .gitignore), THEN scan the full staged diff — the
        # only way to also catch secrets in NEW (untracked) files. Unstage if the
        # guard trips, so we leave the repo exactly as we found it.
        git("add", "-A")
        staged_names = git("diff", "--cached", "--name-only").stdout
        staged_diff = git("diff", "--cached").stdout
        bad_name = next((f for f in staged_names.splitlines() if SECRET_NAMES.search(f)), None)
        if bad_name:
            git("reset", "-q")
            report.append(f"• {rp}: ⚠️ BLOCKED — a secret/PII file ({bad_name}) is staged; "
                          f"add it to .gitignore. I won't push this.")
            continue
        if SECRET_CONTENT.search(staged_diff):
            git("reset", "-q")
            report.append(f"• {rp}: ⚠️ BLOCKED — the diff contains something shaped like a real "
                          f"key/token. Review by hand; I won't push this.")
            continue
        n = len(status.splitlines())
        commit = git("commit", "-m", f"chore: pais auto-sync ({n} file(s))")
        if commit.returncode != 0:
            report.append(f"• {rp}: commit failed — {commit.stderr.strip()[:120]}")
            continue
        push = git("push")
        if push.returncode != 0 and "no upstream" in (push.stderr or "").lower():
            push = git("push", "-u", "origin", "HEAD")     # first push of a new branch
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
            msgs = client.messages().get("messages", [])
            # Window = everything SINCE THE LAST BRIEF, not a rigid 24h. A late run
            # (e.g. 06-22 fired at 08:22 after a battery wake) pushed yesterday's work
            # just outside a fixed 24h cutoff and the brief went blank. Anchoring to
            # the previous briefing means a late start still quotes the real activity.
            prior_briefs = [m.get("ts", 0) for m in msgs if m.get("agent") == "briefing"]
            cutoff = max(prior_briefs) if prior_briefs else (datetime.now().timestamp() - 86400) * 1000
            recent = [m for m in msgs[-60:]
                      if m.get("ts", 0) > cutoff and m.get("agent") != "briefing"]
            feed = "\n\n".join(f"[{m['agent']}] {m['text'][:600]}" for m in recent)[:6000]
        except Exception:
            feed = ""
    today = datetime.now().strftime("%A, %B %d")
    prompt = (
        f"Write the user's daily brief for {today}.\n"
        f"{_settings_block(persona, fields)}\n"
        f"WHAT THEIR AGENT TEAM DID IN THE LAST RUN (their real feed — quote from it, "
        f"never invent):\n{feed or '(no agent activity in the last run)'}\n\n"
        f"TIMING — IMPORTANT: you run FIRST each morning, before today's agents "
        f"(jobs, outreach, etc.) execute. The feed above is the PREVIOUS run's "
        f"completed work, not a live snapshot of today. Frame it as 'what your team "
        f"finished last run / overnight'. Do NOT present pipeline counts (e.g. "
        f"'N applications filled') as today's live state — today's numbers will be "
        f"set by the agents that run after this brief.\n"
        f"{DESCRIPTIVE}\nLead with the single most important item, then: what happened, "
        f"what's open, what matters next — with the exact next action for each thread."
    )
    # briefing is the FIRST agent every run, so it absorbs any cold-start latency
    # (the first `claude -p` after a wake). _claude already retries with backoff and
    # kills a hung process group, and the routine warms the CLI before any agent runs
    # — so give the brief headroom and let those guards handle a slow first call.
    return _claude(prompt, timeout=480)


# ── sales: local-business prospecting into the call sheet ─────────────────────
def run_sales(secrets: dict, fields: dict, persona: str = "") -> dict:
    """Sales prospecting — runs the SAME script the on-demand Sales button runs
    (~/agentic_os/sales_agent.py): researches local businesses in the Royersford↔KOP
    corridor via claude, dedupes against the call sheet, and appends new leads as
    '🟣 To call' to the vault Piontrix Sales Pipeline. Returns its summary for the
    feed. Reusing the one script keeps scheduled + on-demand identical."""
    script = Path.home() / "agentic_os" / "sales_agent.py"
    if not script.exists():
        return {"actionable": False,
                "text": "Sales agent script not found (~/agentic_os/sales_agent.py)."}
    env = {**os.environ, "ANTHROPIC_MODEL": "claude-sonnet-4-6"}   # cost pin (same as bridge)
    if (persona or "").strip():
        env["PAIS_PERSONA"] = persona
    if fields:
        env["PAIS_FIELDS"] = json.dumps(fields, ensure_ascii=False)
    try:
        proc = subprocess.run([sys.executable, str(script)], cwd=str(script.parent),
                              capture_output=True, text=True, timeout=540, env=env)
    except subprocess.TimeoutExpired:
        return {"actionable": False, "text": "Sales agent timed out after 540s."}
    if proc.returncode != 0:
        # Keep the TAIL, not the head: a Python traceback puts the actual
        # exception type+message at the END, so [:300] threw away the one line
        # that explained the failure (and masked the 06-25 iCloud-eviction crash).
        err = (proc.stderr or "").strip()[-600:] or (proc.stdout or "").strip()[-600:] or "sales_agent failed"
        return {"actionable": False, "text": f"Sales agent failed: {err}"}
    out = (proc.stdout or "").strip()
    return {"text": out or "Sales agent ran — no new prospects today.", "actionable": bool(out)}


def run_assistant(secrets: dict, fields: dict, persona: str = "") -> str:
    return ("Control Room online. Chat with me on the website — your other agents run "
            "here on your machine and post their work to this feed.")


# agent id → runner (mirrors the n8n / morning_stack workflows)
RUNNERS = {
    "jobs":      run_jobs,        # MERGED: scout fresh roles → pipeline → Gemini fill
    "career":    run_jobs,        # back-compat alias (old routine order) → merged agent
    "apply":     run_apply,       # back-compat: fill queued 'To apply' rows (no scout)
    "briefing":  run_briefing,    # feed-grounded daily brief
    "email":     run_email,       # read-only Gmail IMAP triage
    "outreach":  run_outreach,    # prospect + draft for review (never sends)
    "linkedin":  run_linkedin,    # 1 connect draft per run (sent by hand)
    "sales":     run_sales,       # local-business prospecting → call sheet
    "code":      run_code,        # guarded git sync
    "assistant": run_assistant,
}


def run_agent(agent: str, secrets: dict, fields: dict, persona: str = "",
              client=None) -> tuple[str, bool]:
    """Run a teammate and return (text_for_feed, actionable). A runner may return a
    plain string (always treated as actionable) or a dict {text, actionable} so it
    can signal that it ran but produced nothing the user can act on yet."""
    runner = RUNNERS.get(agent)
    if not runner:
        raise RuntimeError(f"No runner for agent '{agent}'.")
    if runner is run_briefing:
        result = runner(secrets, fields, persona, client=client)
    else:
        result = runner(secrets, fields, persona)
    if isinstance(result, dict):
        return result.get("text", ""), bool(result.get("actionable", True))
    return result, True

#!/usr/bin/env python3
"""
PAIS desktop runtime (scaffold).

The local half of PAIS: it authenticates as you, pulls the agents you configured
on the web (schedules + the secrets they need), runs them on this machine, and
installs launchd jobs so scheduled agents fire automatically — the same model as
the morning-stack, but driven by your web config instead of hand-edited files.

Usage:
    python runtime.py link <code>                       # connect with a durable device token (preferred)
    python runtime.py login <supabase_refresh_token>    # legacy connect (rotating browser token)
    python runtime.py status                            # show your routine + connections
    python runtime.py routine                           # run the whole routine now, in order
    python runtime.py run <agent>                       # run one workflow now
    python runtime.py daemon                            # run the persistent scheduler loop (installed by `schedule`)
    python runtime.py schedule                          # install the always-on runtime daemon
    python runtime.py unschedule                        # remove it

The routine runs your stacked workflows sequentially (the local mirror of
morning_stack.sh): each is guarded so one failure never stops the chain.

Scheduling model (the n8n-reliability model, native — no n8n dependency): a
persistent KeepAlive launchd *daemon* runs an internal once-a-minute loop that
fires the routine when it's due and hasn't already succeeded today. Unlike a
one-shot StartCalendarInterval job, this CATCHES UP after sleep — if the Mac was
asleep at the scheduled time, the routine runs the moment it's next awake.

Credentials/state live in ~/.pais/ (0600). Secrets are fetched per-run over TLS
and never written to disk in clear.
"""

import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from client import PaisClient, NotLoggedIn, API_BASE

LABEL_PREFIX = "com.pais.agent."      # legacy per-agent jobs (cleaned up on schedule)
ROUTINE_LABEL = "com.pais.routine"    # the single runtime-daemon job
LAUNCH_DIR = Path.home() / "Library" / "LaunchAgents"
RUNTIME = Path(__file__).resolve()
PY = sys.executable
AGENTIC_ENV = Path.home() / "agentic_os" / ".env"   # Telegram bot creds live here
STATE_FILE = Path.home() / ".pais" / "runtime_state.json"   # daemon run-state (0600)
ATTEMPT_COOLDOWN_S = 1800             # min gap between routine attempts after a failure


def _telegram(text: str) -> None:
    """Send a plain-text Telegram message (no parse_mode → no HTML-escaping
    pitfalls). Reads bot creds from agentic_os/.env; silent if unconfigured.
    Truncates to Telegram's limit. Never raises."""
    try:
        creds = {}
        for line in AGENTIC_ENV.read_text().splitlines():
            if line.startswith(("TELEGRAM_BOT_TOKEN=", "TELEGRAM_CHAT_ID=")):
                k, _, v = line.partition("=")
                creds[k] = v.strip().strip('"').strip("'")
        token, chat = creds.get("TELEGRAM_BOT_TOKEN"), creds.get("TELEGRAM_CHAT_ID")
        if not (token and chat):
            return
        import requests
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text[:4000]}, timeout=10)
    except Exception:
        pass  # messaging must never break (or mask) the real work


def _telegram_alert(text: str) -> None:
    """Failure ping (kept for the unattended-failure call sites)."""
    _telegram(text)


def _tg_agent(agent: str, text: str) -> None:
    """Each agent messages Telegram with its update (mirrors the web feed).
    Long reports are truncated with a pointer to the full feed."""
    body = text if len(text) <= 3800 else text[:3800] + "\n\n…(full report in your web feed)"
    _telegram(f"🤖 {agent} — {datetime.now().strftime('%b %d')}\n\n{body}")


def _telegram_long(text: str) -> None:
    """Send the FULL text to Telegram, split into <=4000-char messages on line
    boundaries (Telegram hard-caps one message at 4096). Used where the whole
    report matters — the reviewer's scheduled digest and individual manual runs."""
    LIMIT = 3900
    chunks, cur = [], ""
    for line in (text or "").split("\n"):
        while len(line) > LIMIT:                    # a single oversized line
            if cur:
                chunks.append(cur); cur = ""
            chunks.append(line[:LIMIT]); line = line[LIMIT:]
        if cur and len(cur) + len(line) + 1 > LIMIT:
            chunks.append(cur); cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        chunks.append(cur)
    total = len(chunks) or 1
    for i, ch in enumerate(chunks, 1):
        _telegram(ch if total == 1 else f"{ch}\n\n({i}/{total})")


def _tg_agent_full(agent: str, text: str) -> None:
    """Like _tg_agent but sends the COMPLETE output (chunked), never truncated."""
    _telegram_long(f"🤖 {agent} — {datetime.now().strftime('%b %d')}\n\n{text}")


# ── cron → launchd StartCalendarInterval ──────────────────────────────────────
def cron_to_calendar(expr: str):
    """Convert the supported preset cron shapes to launchd intervals.
    Handles 'm h * * dow' where dow is *, a list (1,3,6) or a range (1-5)."""
    parts = expr.split()
    if len(parts) != 5:
        return None
    minute, hour, _dom, _mon, dow = parts
    base = {}
    if minute != "*":
        base["Minute"] = int(minute)
    if hour != "*":
        base["Hour"] = int(hour)
    if dow == "*":
        return [base]
    days = []
    for token in dow.split(","):
        if "-" in token:
            a, b = map(int, token.split("-"))
            days.extend(range(a, b + 1))
        else:
            days.append(int(token))
    return [{**base, "Weekday": d} for d in days]


# ── daemon state + due check ──────────────────────────────────────────────────
def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))
    try:
        os.chmod(STATE_FILE, 0o600)
    except Exception:
        pass


def _cron_fields(expr: str):
    """(minute, hour, weekdays|None) from 'm h * * dow'. weekdays in cron form
    (Sun=0/7 … Sat=6); None means every day."""
    parts = (expr or "").split()
    if len(parts) != 5:
        return None
    m, h, _dom, _mon, dow = parts
    minute = int(m) if m.isdigit() else 0
    hour = int(h) if h.isdigit() else 0
    if dow == "*":
        return minute, hour, None
    wd = set()
    for token in dow.split(","):
        if "-" in token:
            a, b = map(int, token.split("-"))
            wd.update(range(a, b + 1))
        elif token.isdigit():
            wd.add(int(token))
    if 7 in wd:                 # cron allows 7 for Sunday; normalise to 0
        wd.add(0)
    return minute, hour, wd


def _routine_due(cron: str, now: datetime, state: dict,
                 cooldown: int = ATTEMPT_COOLDOWN_S) -> bool:
    """True if the routine should fire now: today is an allowed weekday, we're
    past the scheduled time, it hasn't already succeeded today, and we're not
    inside the post-failure cooldown. This is what gives sleep catch-up — being
    'past the time' (not 'at the time') means a missed 7:30 still fires later."""
    fields = _cron_fields(cron) or (30, 7, None)
    minute, hour, wd = fields
    today = now.strftime("%Y-%m-%d")
    if state.get("last_success_date") == today:
        return False
    if wd is not None and (now.isoweekday() % 7) not in wd:   # isoweekday%7: Sun=0…Sat=6
        return False
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < scheduled:
        return False
    if time.time() - state.get("last_attempt_ts", 0) < cooldown:
        return False
    return True


# ── commands ──────────────────────────────────────────────────────────────────
def cmd_link(code: str = ""):
    """Connect this machine with a DURABLE, non-rotating device token (preferred
    over `login`, which borrows the web app's rotating Supabase session and gets
    invalidated whenever you sign out of the browser).

    Flow: open /app → "Connect this device" → it shows a short code → run
    `pais link <code>` here. The backend mints a long-lived runtime token tied to
    your account; only the short-lived access token rotates after that.

    Backend endpoints (LIVE on api.* since 2026-06-13):
      POST /api/runtime/link-code  (authed web user)  -> {code, expires_in}
      POST /api/runtime/link    {code}                 -> {device_token}
      POST /api/runtime/session Bearer <device_token>  -> {access_token, expires_in}
    `login` (rotating Supabase token) still works as a fallback.
    """
    if not code:
        print(cmd_link.__doc__)
        return
    import requests
    r = requests.post(f"{API_BASE}/api/runtime/link",
                      json={"code": code.strip()}, timeout=20)
    r.raise_for_status()
    PaisClient.save_device_token(r.json()["device_token"])
    print("✓ Device linked with a durable runtime token.")


def cmd_login(token: str):
    PaisClient.login(token)
    c = PaisClient()
    try:
        me = c.whoami()
        print(f"✓ Connected as {me.get('email', me.get('id', 'your account'))}.")
    except Exception:
        print("✓ Token saved. (Could not verify now — run `status` once online.)")


def cmd_status():
    c = PaisClient()
    cfg = c.config()
    conns = cfg.get("connections", {})
    rt = cfg.get("routine", {})
    order = rt.get("order", [])
    print("Connections:")
    for k, v in conns.items():
        print(f"  {k}: {'✓ set' if v else '—'}")
    print(f"\nMorning routine — runs {rt.get('cron', '(unset)')}:")
    if not order:
        print("  (empty — stack workflows at /app)")
    agents = cfg.get("agents", {})
    for i, aid in enumerate(order, 1):
        a = agents.get(aid, {})
        print(f"  {i}. {aid:10} enabled={a.get('enabled', False)}")


def _remove_jobs(prefix: str) -> int:
    removed = 0
    for path in LAUNCH_DIR.glob(prefix + "*.plist"):
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
        path.unlink()
        removed += 1
    return removed


def cmd_run(agent: str):
    """Run a single teammate now and post its update to the website feed."""
    import agents as runners
    c = PaisClient()
    sec = c.secrets().get("connections", {})
    acfg = c.config().get("agents", {}).get(agent, {}) or {}
    text, actionable = runners.run_agent(agent, sec, acfg.get("fields", {}),
                                         persona=acfg.get("persona", ""), client=c)
    c.post_message(agent, text)
    _tg_agent_full(agent, text)                    # → Telegram, FULL output (manual run)
    note = "" if actionable else " (ran, but no actionable output — check its settings)"
    print(f"✓ {agent}: posted to your website feed + Telegram{note}")


def cmd_routine(scheduled: bool = False):
    """
    Run the whole morning routine, in order — the local mirror of morning_stack.sh.
    Each workflow is guarded so one failure never stops the chain; a summary is
    printed (and Telegrammed if a bot is configured).

    Telegram policy:
      - scheduled run (daemon, PAIS_SCHEDULED=1): ONLY the reviewer messages you,
        with its complete audit — no per-agent pings, so the morning is one digest.
      - manual `routine`: each agent still pings (the prior behavior) for visibility
        while you watch it run by hand.
    Either way the reviewer's report is sent in FULL (chunked past the 4096 cap),
    and every agent always posts to the website feed.
    """
    # Hold the Mac awake for the whole routine. A DarkWake/clamshell window
    # otherwise drops back to sleep mid-run and truncates it (the 06-13 failure).
    # Re-exec once under caffeinate; the env guard prevents a loop, and it no-ops
    # where caffeinate is absent (non-mac). The daemon runs us as a SUBPROCESS,
    # so this exec replaces the routine process only — never the daemon loop.
    # -d keeps the DISPLAY awake too: the apply agent's Gemini-in-Chrome fill is
    # GUI automation (AppleScript keystrokes + window capture) that needs a lit
    # screen. The assertion releases when this process exits — i.e. right after
    # the reviewer sends its Telegram report — so the display then sleeps normally.
    if os.environ.get("PAIS_CAFFEINATED") != "1" and shutil.which("caffeinate"):
        os.environ["PAIS_CAFFEINATED"] = "1"
        os.execvp("caffeinate", ["caffeinate", "-dimsu", PY, str(RUNTIME), "routine"])

    import agents as runners
    c = PaisClient()
    # Auth/backend pre-check: fail fast with ONE clear alert instead of letting a
    # rejected token or unreachable backend take the whole run down silently. Before
    # this, config()/secrets() threw uncaught at the top — the daemon logged a
    # traceback, nothing hit Telegram, and Taran just saw a zero-run with no reason
    # why (the 06-12/13 failures). The alert fires even on scheduled runs: a total
    # auth failure is exactly when you need to know, reviewer-only gating aside.
    try:
        c._access_token()                       # validate auth first (precise error)
        cfg = c.config()
        sec = c.secrets().get("connections", {})
    except NotLoggedIn as e:
        msg = f"⚠️ PAIS morning routine aborted — auth failed: {e}"
        print(msg, file=sys.stderr); _telegram_alert(msg)
        return
    except Exception as e:                       # backend down / network / timeout
        msg = f"⚠️ PAIS morning routine aborted — couldn't reach the backend: {str(e)[:200]}"
        print(msg, file=sys.stderr); _telegram_alert(msg)
        return
    agents_cfg = cfg.get("agents", {})
    order = [a for a in cfg.get("routine", {}).get("order", []) if a]
    if not order:
        print("Routine is empty — nothing to run.")
        return
    # apply REJOINED the routine 2026-06-16 (testing the full pipeline end-to-end).
    # It opens Gemini-fill windows, so it needs the Mac awake with a GUI session.
    # Reviewer runs LAST (on the backend) so it can grade the others' fresh output.
    #
    # The backend's canonical order can include agents this runtime has no local
    # runner for (e.g. 'sales' is defined backend-side but unimplemented here),
    # which used to throw "No runner for agent" on every run. Skip those up front
    # so a config-only agent never spams a daily failure — the reviewer is exempt
    # because it runs on the backend, not through runners.RUNNERS.
    skipped = [a for a in order if a not in ("reviewer",) and a not in runners.RUNNERS]
    if skipped:
        print(f"  ⤷ skipping {', '.join(skipped)}: no local runner (configured backend-side only)")
    run_order = [a for a in order if a != "reviewer" and a in runners.RUNNERS]
    print(f"▶ Morning routine: {' → '.join(run_order)} → reviewer")
    # Warm the `claude` CLI BEFORE any real agent. The routine fires in the fragile
    # minutes right after a battery-sleep wake; the first `claude -p` then is the one
    # that hangs or dies cold (the 06-22 failure took out briefing + outreach). Spend
    # a cheap throwaway probe on that cold start so the real agents hit a warm daemon.
    if not runners.warm_up_claude():
        print("  ⚠ claude CLI did not warm up after several tries — running anyway", file=sys.stderr)
    # Scheduled runs are reviewer-only on Telegram, but these agents produce a
    # ready-to-act deliverable (e.g. Gmail drafts) that's worthless if it only
    # lands on the web feed — so they always reach the phone when actionable.
    ALWAYS_TG = {"outreach"}
    ok = 0
    for aid in run_order:
        acfg = agents_cfg.get(aid, {}) or {}
        try:
            text, actionable = runners.run_agent(aid, sec, acfg.get("fields", {}),
                                                  persona=acfg.get("persona", ""), client=c)
            c.post_message(aid, text)              # → website feed (always)
            if not scheduled:                      # scheduled = reviewer-only Telegram …
                _tg_agent(aid, text)
            elif aid in ALWAYS_TG and actionable:  # … except ready-to-act deliverables
                _tg_agent_full(aid, text)
            ok += 1
            print(f"  ✓ {aid}: posted" if actionable
                  else f"  ⚠ {aid}: ran, no actionable output")
        except Exception as e:
            print(f"  ✗ {aid}: {e}", file=sys.stderr)
            if not scheduled:                      # on schedule the reviewer flags failures
                _telegram(f"⚠️ {aid} failed in the morning routine: {str(e)[:300]}")
    def _latest_rev_ts() -> float:
        """Timestamp of the newest reviewer audit, 0.0 if none — used to tell a
        FRESH audit from a stale one so we never re-send yesterday's report."""
        try:
            msgs = c.messages(agent="reviewer").get("messages", [])
            if not msgs:
                return 0.0
            return float(msgs[-1].get("created_at") or msgs[-1].get("ts") or 0)
        except Exception:
            return 0.0

    try:
        before_ts = _latest_rev_ts()               # snapshot BEFORE the audit runs
        res = c.run_backend_agent("reviewer")      # audits the run via the backend
        # HTTP 200 with ran=False = backend reached the reviewer but it produced
        # no audit (e.g. the bridge/claude was session-limited). Don't claim
        # success, and DON'T mirror a stale report as if it were today's.
        if isinstance(res, dict) and res.get("ran") is False:
            why = str(res.get("message") or "no audit produced")[:200]
            print(f"  ⚠ reviewer: ran but produced no audit — {why}", file=sys.stderr)
            _telegram(f"⚠️ Reviewer produced no audit this run — {why}. "
                      f"Not re-sending an older report.")
        else:
            print("  ✓ reviewer: audited the run")
            try:                                   # mirror ONLY a genuinely fresh report
                rev = c.messages(agent="reviewer").get("messages", [])
                newest = rev[-1] if rev else None
                newest_ts = (float(newest.get("created_at") or newest.get("ts") or 0)
                             if newest else 0.0)
                if newest and newest_ts > before_ts:
                    _tg_agent_full("reviewer", newest.get("text", ""))
                else:
                    print("  ⚠ reviewer: no NEW audit in feed — skipping stale mirror",
                          file=sys.stderr)
            except Exception:
                pass
    except Exception as e:
        print(f"  ✗ reviewer: {e}", file=sys.stderr)
    print(f"Routine done — {ok}/{len(run_order)} posted, then audited.")


def _hold_awake(on: bool) -> None:
    """Keep the Mac awake across a routine run even on BATTERY. caffeinate's
    PreventSystemSleep is ignored on battery; `pmset disablesleep` is the
    kernel-level override that is not. Best-effort via `sudo -n` — silently
    no-ops until the one-time pmset sudoers rule is installed (see README),
    then it takes effect. ALWAYS reset to 0 after the run, or the Mac never sleeps."""
    try:
        subprocess.run(["sudo", "-n", "pmset", "-a", "disablesleep",
                        "1" if on else "0"], capture_output=True, timeout=10)
    except Exception:
        pass


def cmd_daemon():
    """Persistent scheduler loop — the always-on half of the runtime.

    Every 60s: refresh the cron from the web config (hourly, tolerating
    offline/auth hiccups), then if the routine is due and hasn't succeeded today,
    run it as a SUBPROCESS (so it can re-exec under caffeinate without touching
    this loop). On macOS the loop is frozen while the Mac sleeps and resumes on
    wake — so a routine missed at 7:30 fires the moment the machine is next awake.
    """
    print(f"[daemon] started pid={os.getpid()} — checking every 60s", flush=True)
    _hold_awake(False)                    # clear any stale disablesleep from a prior crash
    cron = "30 7 * * *"
    cron_checked = 0.0
    while True:
        time.sleep(60)
        now = datetime.now()
        if time.time() - cron_checked > 3600:        # refresh cron at most hourly
            try:
                cron = PaisClient().config().get("routine", {}).get("cron") or cron
            except Exception:
                pass                                  # keep last-known / default
            cron_checked = time.time()
        try:
            state = _load_state()
            if not _routine_due(cron, now, state):
                continue
            print(f"[daemon] routine due (cron='{cron}') — running", flush=True)
            state["last_attempt_ts"] = time.time()
            _save_state(state)
            _hold_awake(True)             # lock awake even on battery for the run
            try:
                # PAIS_SCHEDULED marks this as the unattended run → reviewer-only
                # Telegram. It survives the caffeinate execvp (inherited env), so
                # the flag reaches cmd_routine even after the re-exec.
                r = subprocess.run([PY, str(RUNTIME), "routine"],
                                   env={**os.environ, "PAIS_SCHEDULED": "1"}, timeout=3600)
            finally:
                _hold_awake(False)        # always release — else the Mac never sleeps
            if r.returncode == 0:
                state = _load_state()
                state["last_success_date"] = now.strftime("%Y-%m-%d")
                _save_state(state)
                print("[daemon] routine completed", flush=True)
            else:
                print(f"[daemon] routine exited {r.returncode} — will retry after "
                      f"cooldown", file=sys.stderr, flush=True)
        except subprocess.TimeoutExpired:
            print("[daemon] routine timed out (>1h) — will retry after cooldown",
                  file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[daemon] loop error: {e}", file=sys.stderr, flush=True)


def cmd_schedule():
    """Install the always-on runtime daemon (KeepAlive launchd job).

    Replaces the old one-shot StartCalendarInterval job: a one-shot fire at the
    scheduled minute is silently SKIPPED on any day the Mac is asleep then. The
    daemon's internal loop instead catches up on the next wake.
    """
    c = PaisClient()
    rt = c.config().get("routine", {})
    order = [a for a in rt.get("order", []) if a]
    if not order:
        print("Your morning routine is empty — stack workflows at /app first.")
        return
    if not cron_to_calendar(rt.get("cron", "30 7 * * *")):
        print(f"Unsupported schedule '{rt.get('cron')}'.")
        return
    _remove_jobs(LABEL_PREFIX)            # clear any legacy per-agent jobs
    LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    # PATH must reach `claude` (subscription CLI) + python so launchd runs find them.
    dirs = [os.path.dirname(shutil.which("claude") or ""), os.path.dirname(PY),
            "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    path_env = ":".join(d for d in dict.fromkeys(dirs) if d)
    plist = {
        "Label": ROUTINE_LABEL,
        "ProgramArguments": [PY, str(RUNTIME), "daemon"],
        "RunAtLoad": True,            # start now + on every login
        "KeepAlive": True,            # relaunch if it ever dies (n8n-style always-on)
        "StandardOutPath": str(Path.home() / ".pais" / "daemon.out.log"),
        "StandardErrorPath": str(Path.home() / ".pais" / "daemon.err.log"),
        "EnvironmentVariables": {"PATH": path_env},
    }
    plist_path = LAUNCH_DIR / f"{ROUTINE_LABEL}.plist"
    with open(plist_path, "wb") as f:
        plistlib.dump(plist, f)
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True)
    print(f"✓ Installed PAIS runtime daemon — runs your routine at "
          f"{rt.get('cron', '30 7 * * *')} (catches up after sleep).")
    print(f"  order: {' → '.join(order)}")


def cmd_unschedule():
    removed = _remove_jobs(LABEL_PREFIX)
    p = LAUNCH_DIR / f"{ROUTINE_LABEL}.plist"
    if p.exists():
        subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
        p.unlink(); removed += 1
    print(f"Removed {removed} launchd job(s).")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 0
    cmd, rest = args[0], args[1:]
    try:
        if cmd == "link":
            cmd_link(rest[0] if rest else "")
        elif cmd == "login" and rest:
            cmd_login(rest[0])
        elif cmd == "status":
            cmd_status()
        elif cmd == "run" and rest:
            cmd_run(rest[0])
        elif cmd == "routine":
            cmd_routine(scheduled=os.environ.get("PAIS_SCHEDULED") == "1")
        elif cmd == "daemon":
            cmd_daemon()
        elif cmd == "schedule":
            cmd_schedule()
        elif cmd == "unschedule":
            cmd_unschedule()
        else:
            print(__doc__)
            return 2
    except NotLoggedIn as e:
        print(f"⚠️  {e}")
        if cmd == "routine":   # unattended morning run — don't fail silently
            _telegram_alert(f"PAIS morning routine did NOT run: {e}\n"
                            "Fix: pais login <fresh token>, then sign out of the "
                            "web tab the token came from.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

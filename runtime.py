#!/usr/bin/env python3
"""
PAIS desktop runtime (scaffold).

The local half of PAIS: it authenticates as you, pulls the agents you configured
on the web (schedules + the secrets they need), runs them on this machine, and
installs launchd jobs so scheduled agents fire automatically — the same model as
the morning-stack, but driven by your web config instead of hand-edited files.

Usage:
    python runtime.py login <supabase_refresh_token>   # one-time connect
    python runtime.py status                            # show your routine + connections
    python runtime.py routine                           # run the whole routine now, in order
    python runtime.py run <agent>                       # run one workflow now
    python runtime.py schedule                          # install the single morning-routine launchd job
    python runtime.py unschedule                        # remove it

The routine runs your stacked workflows sequentially (the local mirror of
morning_stack.sh): each is guarded so one failure never stops the chain.

Credentials/state live in ~/.pais/ (0600). Secrets are fetched per-run over TLS
and never written to disk in clear.
"""

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from client import PaisClient, NotLoggedIn

LABEL_PREFIX = "com.pais.agent."      # legacy per-agent jobs (cleaned up on schedule)
ROUTINE_LABEL = "com.pais.routine"    # the single morning-routine job
LAUNCH_DIR = Path.home() / "Library" / "LaunchAgents"
RUNTIME = Path(__file__).resolve()
PY = sys.executable


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


# ── commands ──────────────────────────────────────────────────────────────────
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
    text = runners.run_agent(agent, sec, acfg.get("fields", {}),
                             persona=acfg.get("persona", ""), client=c)
    c.post_message(agent, text)
    print(f"✓ {agent}: posted to your website feed")


def cmd_routine():
    """
    Run the whole morning routine, in order — the local mirror of morning_stack.sh.
    Each workflow is guarded so one failure never stops the chain; a summary is
    printed (and Telegrammed if a bot is configured).
    """
    import agents as runners
    c = PaisClient()
    cfg = c.config()
    sec = c.secrets().get("connections", {})
    agents_cfg = cfg.get("agents", {})
    order = [a for a in cfg.get("routine", {}).get("order", []) if a]
    if not order:
        print("Routine is empty — nothing to run.")
        return
    # Reviewer runs LAST (on the backend) so it can grade the others' fresh output.
    run_order = [a for a in order if a != "reviewer"]
    print(f"▶ Morning routine: {' → '.join(run_order)} → reviewer")
    ok = 0
    for aid in run_order:
        acfg = agents_cfg.get(aid, {}) or {}
        try:
            text = runners.run_agent(aid, sec, acfg.get("fields", {}),
                                     persona=acfg.get("persona", ""), client=c)
            c.post_message(aid, text)              # → website feed (no Telegram)
            ok += 1; print(f"  ✓ {aid}: posted")
        except Exception as e:
            print(f"  ✗ {aid}: {e}", file=sys.stderr)
    try:
        c.run_backend_agent("reviewer")            # audits the run via the backend
        print("  ✓ reviewer: audited the run")
    except Exception as e:
        print(f"  ✗ reviewer: {e}", file=sys.stderr)
    print(f"Routine done — {ok}/{len(run_order)} posted, then audited.")


def cmd_schedule():
    """Install ONE launchd job that runs the routine in order at its scheduled time."""
    c = PaisClient()
    rt = c.config().get("routine", {})
    order = [a for a in rt.get("order", []) if a]
    if not order:
        print("Your morning routine is empty — stack workflows at /app first.")
        return
    intervals = cron_to_calendar(rt.get("cron", "30 7 * * *"))
    if not intervals:
        print(f"Unsupported schedule '{rt.get('cron')}'.")
        return
    _remove_jobs(LABEL_PREFIX)            # clear any legacy per-agent jobs
    LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    # PATH must reach `claude` (subscription CLI) + python so launchd runs find them.
    dirs = [os.path.dirname(shutil.which("claude") or ""), os.path.dirname(PY),
            "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    path = ":".join(d for d in dict.fromkeys(dirs) if d)
    plist = {
        "Label": ROUTINE_LABEL,
        "ProgramArguments": [PY, str(RUNTIME), "routine"],
        "StartCalendarInterval": intervals if len(intervals) > 1 else intervals[0],
        "StandardOutPath": str(Path.home() / ".pais" / "routine.out.log"),
        "StandardErrorPath": str(Path.home() / ".pais" / "routine.err.log"),
        "EnvironmentVariables": {"PATH": path},
    }
    path = LAUNCH_DIR / f"{ROUTINE_LABEL}.plist"
    with open(path, "wb") as f:
        plistlib.dump(plist, f)
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    subprocess.run(["launchctl", "load", str(path)], capture_output=True)
    print(f"✓ Scheduled your morning routine ({len(order)} workflow(s)) at {rt.get('cron')}.")
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
        if cmd == "login" and rest:
            cmd_login(rest[0])
        elif cmd == "status":
            cmd_status()
        elif cmd == "run" and rest:
            cmd_run(rest[0])
        elif cmd == "routine":
            cmd_routine()
        elif cmd == "schedule":
            cmd_schedule()
        elif cmd == "unschedule":
            cmd_unschedule()
        else:
            print(__doc__)
            return 2
    except NotLoggedIn as e:
        print(f"⚠️  {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

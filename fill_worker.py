#!/usr/bin/env python3
"""Detached job-application fill worker.

Spawned by the jobs agent (agents.run_jobs) so the Playwright fill OUTLIVES the
morning routine. The routine exits a few minutes after it finishes (right after
the reviewer), which would kill any in-process fill and close the browser before
you ever saw it — that's why the 06-22 scheduled run fell back to just opening
tabs. Running the fill in a separate, session-detached process fixes that: it
fills each queued job in ONE Chromium, marks verified fills Applied in the vault
Job Pipeline, posts a per-application result line to the website feed when done,
and keeps the browser open for manual review. It NEVER submits.

Invoked as:  python fill_worker.py <payload.json>
where payload.json = {"batch": [<job dicts>], "keep_open": <seconds>}
"""
import json
import sys
from pathlib import Path


def _emit(client, text: str) -> None:
    if client is None:
        return
    try:
        client.post_message("jobs", text)
    except Exception:
        pass


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: fill_worker.py <payload.json>", file=sys.stderr)
        return
    payload = json.loads(Path(sys.argv[1]).read_text())
    batch = payload.get("batch", [])
    keep = int(payload.get("keep_open", 1800))
    if not batch:
        return

    # ~/agentic_os hosts the Playwright filler + the shared job sheet; ~/pais-runtime
    # hosts the feed client. Put both on the path before importing either.
    for p in (str(Path.home() / "agentic_os"), str(Path.home() / "pais-runtime")):
        if p not in sys.path:
            sys.path.insert(0, p)

    from tools.pais_browser import browser_fill_pw_batch  # type: ignore

    try:
        from tools import job_sheet  # type: ignore
    except Exception:
        job_sheet = None
    try:
        from client import PaisClient
        client = PaisClient()
    except Exception:
        client = None

    results_box: dict[str, dict] = {}

    def _after(results: list[dict]) -> None:
        """Called by the filler once every job is filled (before the browser's
        keep-open hold). Mark Applied + post the real per-application results."""
        for j, r in zip(batch, results):
            results_box[j.get("url", "")] = r
        verified, opened = [], []
        for j in batch:
            company, role, url = j.get("company", "?"), j.get("role", "?"), j.get("url", "")
            r = results_box.get(url) or {}
            if r.get("ok"):
                n = len(r.get("filled", []))
                verified.append(f"• {company} — {role}\n  {url}  ✓ {n} field(s) filled")
                if job_sheet is not None:
                    try:
                        job_sheet.mark_applied(url)
                    except Exception:
                        pass
            else:
                reason = (r.get("error") or "no fillable form / login-walled")[:55]
                opened.append(f"• {company} — {role}\n  {url}  ⚠ finish by hand ({reason})")
        lines = [f"📋 Job fill finished — {len(batch)} application(s) processed."]
        if verified:
            lines.append(f"\n✅ Filled {len(verified)} (review the form, attach your résumé, submit yourself):")
            lines += verified
        if opened:
            lines.append(f"\n🖥️ {len(opened)} need finishing by hand:")
            lines += opened
        lines.append("\n⚠️ Nothing was submitted automatically. The browser stays open for review.")
        _emit(client, "\n".join(lines))

    try:
        browser_fill_pw_batch(batch, keep_open_seconds=keep, after_fill=_after)
    except Exception as e:
        _emit(client, f"📋 Job fill could not run: {str(e)[:200]}\n"
                      "Open your Jobs pipeline and finish these by hand.")


if __name__ == "__main__":
    main()

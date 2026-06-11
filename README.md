# PAIS Desktop Runtime (scaffold)

The local half of PAIS. You set agents up on the web (**/app → an agent → Set up**):
required info, personal secrets (Telegram, API keys), and a schedule or webhook.
This runtime runs on your own machine, pulls that config, and executes the agents
— installing `launchd` jobs so scheduled ones fire automatically.

> **Why a local runtime?** The agents act with *your* accounts (your Telegram bot,
> your Gmail, your keys) on *your* machine — so credentials and execution stay on
> hardware you control. The web app is the control plane; this is the engine.

## Install

```bash
cd ~/pais-runtime
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
```

## Connect it to your account (one-time)

The runtime authenticates as you with a Supabase **refresh token**.

1. Sign in at the web app (`/app`).
2. In the browser console on that page, run:
   ```js
   (await sb.auth.getSession()).data.session.refresh_token
   ```
   *(A "Connect desktop" button that does this for you is the next step — for the
   scaffold this manual copy is fine.)*
3. ```bash
   ./venv/bin/python runtime.py login <paste_refresh_token>
   ```

## Use

```bash
./venv/bin/python runtime.py status            # show your routine (order) + connections
./venv/bin/python runtime.py routine           # run the whole routine now, in order
./venv/bin/python runtime.py run briefing      # run one workflow now (briefing posts to your website feed)
./venv/bin/python runtime.py schedule          # install the single morning-routine launchd job
./venv/bin/python runtime.py unschedule        # remove it
```

## The morning routine

On the web (`/app → Workflows`) you stack workflows like blocks into one ordered
**morning routine** with a single schedule. The runtime mirrors that exactly:

- `schedule` installs **one** launchd job (`com.pais.routine`) at the routine's
  time — not a job per agent.
- When it fires, `routine` runs your team **in order, sequentially** — the local
  twin of `morning_stack.sh`. Each is guarded, so one failure never stops the
  chain, and every teammate **posts its update to your website feed** (no Telegram).
- Toggle agents active / reorder on the web, then re-run `schedule` to apply.

## How it maps to the existing PAIS

Runners in `agents.py` map each web agent to its real PAIS capability:

| Agent     | Maps to                              | Status            |
|-----------|--------------------------------------|-------------------|
| briefing  | posts daily brief to your website    | ✅ wired (real)   |
| career    | `job_scout.py` + `fill_scouted.py`   | scaffold stub     |
| outreach  | `piontrix_outreach.py`               | scaffold stub     |
| assistant | orchestrator general agent           | scaffold stub     |

`briefing` is fully wired (posts to your website feed) to prove the
secrets → action loop. The stubs validate their required connections and report
the capability they map to — port each from `~/agentic_os` as the runtime matures.

## Security

- Credentials + state live in `~/.pais/` with `0600` perms.
- Secrets are fetched per-run over TLS from `/api/agents/secrets` (owner-only) and
  never written to disk in clear.
- `launchd` jobs run `runtime.py tick <agent>`, which re-fetches fresh secrets each run.

## Files

- `client.py` — authenticated API client (refresh-token exchange)
- `agents.py` — per-agent runners
- `runtime.py` — CLI + launchd scheduler (`cron → StartCalendarInterval`)

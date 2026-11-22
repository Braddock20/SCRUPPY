# SCRUPPY on Render (Web Service)

SCRUPPY runs as a Docker-based Render **Web Service**. The HTTP server keeps
Render's web port open while the existing `igbot watch` command runs as a
separate background process. The first deployment remains read-only because
`behaviour.baseline_only` is `true` in `config.yaml`.

## Before deploying

- Use a private Git repository.
- Do not commit `secrets/cookies.txt`, browser profiles, SQLite databases, or logs.
- The earlier source archive contained an Instagram session-cookie file. Do not
  reuse it. Revoke/log out that old session and create a fresh session if you
  decide to test. A session cookie is as sensitive as a password.
- Never paste a session cookie into source code, Git, a README, an issue, or chat.

## Render settings

The checked-in `render.yaml` declares a Docker Web Service and a health check at
`/health`. If configuring the service manually, use:

- Runtime: Docker
- Dockerfile: `./Dockerfile`
- Health check path: `/health`
- Keep the persistent disk mounted at `/app/state` (the Blueprint provisions it).

Set these environment variables in Render:

| Variable | Value |
|---|---|
| `IGBOT_COOKIE` | A fresh session cookie value for your own test account; treat it as a password |
| `IGBOT_CONTROL_TOKEN` | A long, random secret you generate; required for remote start/stop controls |
| `IGBOT_AUTOSTART` | `true` to start the watcher automatically when a cookie is configured |
| `IGBOT_POLL_SECONDS` | `60` |
| `IGBOT_PROFILE_DIR` | `/app/state/profile` |
| `IGBOT_LOG_FILE` | `/app/state/igbot.log` |
| `IGBOT_LOG_LEVEL` | `INFO` |

`IGBOT_COOKIE` accepts the cookie formats supported by the cookie importer.
Do not reuse any cookie previously exposed. If the Render environment editor
cannot safely accept the full cookie value, use a protected secret file and
set `IGBOT_COOKIES_FILE` to its path instead.

## HTTP endpoints

- `GET /` — service information.
- `GET /health` — Render health check; returns `{"status":"ok"}`.
- `GET /status` — secret-free automation status, including whether a session is configured and whether baseline-only mode is active.
- `POST /automation/start` — start the watcher; requires `Authorization: Bearer <IGBOT_CONTROL_TOKEN>`.
- `POST /automation/stop` — stop the watcher and its browser child processes; requires the same authorization header.

Example checks (replace the host and token):

```sh
curl https://YOUR-SERVICE.onrender.com/health
curl https://YOUR-SERVICE.onrender.com/status
curl -X POST https://YOUR-SERVICE.onrender.com/automation/stop \
  -H 'Authorization: Bearer YOUR_CONTROL_TOKEN'
curl -X POST https://YOUR-SERVICE.onrender.com/automation/start \
  -H 'Authorization: Bearer YOUR_CONTROL_TOKEN'
```

If `IGBOT_CONTROL_TOKEN` is not configured, remote start/stop controls return
`503` and remain disabled. The health and status endpoints do not reveal the
cookie or control token.

## Safe first test

- Keep `behaviour.baseline_only: true` in `config.yaml`.
- In this mode the watcher reads and logs inbox activity but does not reply.
  This deployment does not add automatic likes, follows, Story interactions,
  or other engagement actions.
- Confirm `/health` returns HTTP 200 and inspect `/status`.
- Check Render logs for the watcher startup and session validation.
- If Instagram presents a checkpoint, verification, or unusual-activity screen,
  stop the watcher and complete account-security steps directly with Instagram.
  Do not automate or bypass that checkpoint.
- Stop/suspend the service when finished testing to avoid ongoing compute charges.

## Storage and troubleshooting

The Blueprint attaches a 1 GB persistent disk at `/app/state` for the browser
profile, SQLite state, and log file. Persistent disks and the selected compute
plan may incur charges; check current Render pricing.

- `automation: stopped` with `session_configured: false`: configure a fresh
  `IGBOT_COOKIE` or protected cookie file.
- `automation: stopped` with a nonzero `last_exit_code`: inspect
  `/app/state/igbot.log` and Render logs for the worker's startup error.
- `GET /health` fails: confirm the service uses `python -m igbot.web` as its
  Docker command and that the app listens on Render's `PORT`.
- Browser executable missing: confirm the Docker build completed
  `playwright install --with-deps chromium`.
- Exit code 137 / Chromium killed: inspect Render metrics and memory limits
  before retrying.

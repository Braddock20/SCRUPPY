# Render deployment (baseline test)

This repository is configured as a Docker-based Render Background Worker.
It runs Chromium headlessly and starts `igbot watch` in the project's existing
read-only baseline mode (`behaviour.baseline_only: true`).

## Before deploying

- Use a private Git repository.
- Do not commit `secrets/cookies.txt`, browser profiles, SQLite databases, or logs.
- The source ZIP originally supplied for review contained a session-cookie file.
  That file has been removed from this deployment copy. Revoke that old Instagram
  session and use a newly issued session only if you decide to test.
- Never paste a session cookie into source code, Git, a README, an issue, or chat.

## Deploy

1. Push this folder to a **private** GitHub repository.
2. In Render, choose **New > Blueprint** and connect that repository.
3. Render reads `render.yaml` and creates the `igbot-baseline-test` Background Worker.
4. When prompted for `IGBOT_COOKIE`, enter the complete Cookie header from your
   own test account's authenticated browser session. Treat it as a password.
   Do not use the cookie from the supplied ZIP or any cookie previously exposed.
5. Deploy and open the worker's **Logs** tab.

Expected startup indicators include a successful session check, a log line saying
`baseline_only is ON`, and `watching every 60s`.

## Test boundaries

- Keep `behaviour.baseline_only: true` in `config.yaml`.
- This test is intended to read the inbox and log what the bot sees. It must not
  send replies, publish media, follow accounts, or perform engagement actions.
- If Instagram presents a checkpoint, verification, or unusual-activity screen,
  stop the worker and complete any account-security steps directly with Instagram.
  Do not attempt to automate or bypass that checkpoint.
- Stop/suspend the worker when you finish testing to avoid ongoing compute charges.

## Storage and cost

The Blueprint attaches a 1 GB persistent disk at `/app/state` so the browser
profile, SQLite state, and log file survive worker restarts. Render charges for
worker compute and persistent storage. The smallest listed worker compute tier
is paid; Background Workers do not have a free instance. Check the current
Render pricing before deploying.

## Troubleshooting

- `No session cookie found`: confirm `IGBOT_COOKIE` is set in the Render service's
  Environment settings and redeploy.
- `Instagram did not accept that cookie`: do not keep retrying. Stop the worker
  and verify the account session through Instagram directly.
- Browser executable missing: check that the Docker build completed the
  `playwright install --with-deps chromium` step.
- Exit code 137 / Chromium killed: the selected worker may not have enough RAM;
  inspect Render metrics and consider a larger plan before retrying.

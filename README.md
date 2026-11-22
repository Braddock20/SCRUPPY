# igbot — an Instagram DM auto-reply bot that runs 24/7

Your business account, answered around the clock. It reads your Instagram
inbox, works out what each customer wants from a reply book you write, and
answers — slowly, politely, and within limits that make it behave like a
person rather than a robot.

It logs in with a **session cookie**, so there is no password, no two-factor
prompt in the middle of the night, and no lost logins.

---


## Render Web Service

The Docker image starts `python -m igbot.web`, which exposes `/health` and
`/status` while managing the existing `igbot watch` process separately. See
[`RENDER_DEPLOY.md`](RENDER_DEPLOY.md) for Render environment variables, the
Bearer-token-protected start/stop endpoints, and safe baseline testing.

## Read this first: the honest risk

Automating Instagram through a browser is against Instagram's terms of service.
You accept that risk by running this. Here is what the bot does to keep it
small, and what it cannot do:

**Safety and testing behavior**

- The Render deployment is configured for read-only baseline mode. It reads
  the inbox and records what it sees; it does not send replies.
- It uses ordinary Playwright browser behavior and does not mask automation
  signals or attempt to bypass Instagram security checks.
- It stops and waits for you if Instagram displays a checkpoint, identity
  confirmation, or unusual-activity screen. Do not automate that challenge.
- Browser automation may be restricted by Instagram and can stop working when
  the site changes. Use official Meta APIs where they support your use case.

**What it cannot do**

- It cannot guarantee you will not be restricted. Nobody can. If Instagram
  decides your account looks automated, that decision is not appealable by a
  script.
- Instagram will redesign the page eventually and the bot will need a fix.
  The selectors live in **one file** so that is a short job, not a rewrite.
- It cannot reply to someone who has never messaged you. Instagram only lets
  a business reply to a person who has contacted them first — that rule is
  Instagram's, not the bot's.

**Start here:** leave `baseline_only: true` (the default) and run it for a few
days. It reads everything and answers nothing. Watch the log. When you trust
it, flip one line and let it reply.

---

## Quick start

```bash
cd ig-bot
./install.sh
```

That creates a virtual environment, installs two packages and downloads the
browser. It does not touch anything else on your machine.

### 1. Get your Instagram cookie

Your cookie is the key to your account, so treat it like a password. To get
it:

1. Open Instagram in **Chrome or Edge** (not a private window).
2. Log in to your account normally.
3. Press `F12` to open developer tools.
4. Click the **Application** tab (Chrome) or **Storage** tab (Edge).
5. Under **Cookies**, open `https://www.instagram.com`.
6. Copy the **value** of the `sessionid` cookie.

Now build the cookie string. Copy all of these pairs out of the cookie list —
`sessionid` is the important one, the rest make the session look normal:

```
csrftoken=<value>; datr=<value>; ig_did=<value>; mid=<value>; ds_user_id=<value>; sessionid=<value>; rur=<value>
```

> A quicker way: in the developer tools **Console**, paste this and copy the
> result:
> ```js
> copy(document.cookie)
> ```
> That gives you a working cookie string in one step.

Save it into the file `secrets/cookies.txt` (one line, no line breaks), then:

```bash
.venv/bin/python -m igbot login --cookie-file secrets/cookies.txt
```

It will say `Logged in as yourhandle`. The file is stored with owner-only
permissions.

### Supported cookie file formats

`igbot login --cookie-file secrets/cookies.txt` auto-detects:

- Netscape/Mozilla `cookies.txt` exports (seven-column tab-separated format,
  including `#HttpOnly_` rows and per-cookie domain/path/secure/expiry).
- Browser-export JSON arrays, including `{ "cookies": [...] }`.
- A standard `Cookie:` header (`name=value; name=value`).

No conversion is required. Expired Netscape cookies are ignored. Keep cookie
files private: they grant access to your account. Never commit them to Git or
share them in screenshots, chats, or support tickets.

### 2. Check everything works

```bash
.venv/bin/python -m igbot doctor
```

It verifies the cookie, the tabs, the inbox, and prints what it can see.

### 3. Watch (without replying yet)

```bash
.venv/bin/python -m igbot watch
```

### 4. Let it reply

Edit `config.yaml`:

```yaml
behaviour:
  baseline_only: false
```

---

## Commands

| Command | What it does |
|---|---|
| `igbot login` | Connect using a session cookie (`--cookie-file`, `--cookie`, or `--cookie-stdin`) |
| `igbot doctor` | Check the whole setup. `--deep` also tests the send path |
| `igbot baseline` | Read the inbox and print what is there, and what it *would* reply |
| `igbot watch` | The 24/7 loop. `--once` runs a single poll |
| `igbot publish` | Post a photo (`--media`, `--caption`, `--dry-run`) |
| `igbot comments` | Read or reply to comments on a post (`--url`) |
| `igbot stats` | Show how many messages were seen and answered |
| `igbot selftest` | Send a test message to *yourself* to prove sending works |

Every command accepts `--root` to point at a different project folder, and
`--headless` to run without a window (less safe — only for testing).

---

## Configuration

Everything lives in `config.yaml`. The parts you will actually change:

### The reply book

```yaml
reply:
  default_reply: "Thanks for reaching out! We've got your message and will reply shortly."
  rules:
    - name: pricing
      match: ["price", "pricing", "how much", "cost"]
      reply: "Happy to help with pricing! What are you looking to order?"
    - name: shipping
      match: ["shipping", "delivery", "how long"]
      reply: "We ship nationwide. Could you tell me where you're based?"
```

Matching is on **whole words**, so `hi` will not fire on *"shipping"* or
*"child"*. When two rules could match, the **longer, more specific** one wins
— so a rule for `"return shipping"` beats a rule for `"returns"`.

The default reply is used when nothing matches.

### AI replies (optional)

```yaml
reply:
  ai:
    enabled: true
    endpoint: "https://api.openai.com/v1/chat/completions"
    api_key_env: "OPENAI_API_KEY"
    model: "gpt-4o-mini"
```

Any OpenAI-compatible endpoint works. Put the key in an environment variable,
not in the config file.

The AI is only consulted for messages your rules do **not** cover, and if the
AI is down, slow, or rate-limited, the bot falls back to the default reply. A
customer is never left without an answer because a model was unavailable.

### Speed and limits

```yaml
behaviour:
  poll_seconds: 45          # how often to check
  reply_delay_min: 12       # seconds to wait before answering
  reply_delay_max: 55
  cooldown_seconds: 900     # 15 minutes between replies in one conversation
  hourly_cap: 8             # per conversation
  daily_cap: 25             # per conversation
  global_daily_cap: 120     # across all conversations
```

### Message requests

Instagram sends first-time messages to a **Requests** folder, not the main
inbox. Reading one is invisible to the sender. **Accepting** one is not — the
sender is told.

```yaml
behaviour:
  accept_requests: false    # turn on to auto-accept and reply
```

With it off, the bot logs the request for you to answer by hand.

---

## Running it 24/7

On a machine with a screen, just run `igbot watch` in a terminal or under
`tmux`/`screen`.

On a headless server (VPS), use the bundled wrapper, which provides a virtual
display:

```bash
./scripts/igbot-xvfb.sh watch
```

### As a service (systemd)

```bash
sudo mkdir -p /opt/ig-bot
sudo cp -r . /opt/ig-bot
sudo useradd -r -m -d /opt/ig-bot igbot
sudo chown -R igbot:igbot /opt/ig-bot
sudo cp scripts/igbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now igbot
```

Watch it with `journalctl -u igbot -f`.

### Logging in by hand (recommended for the first time)

The bot never scripts the login form — that is the single most
automation-shaped moment in the whole flow. To log in with a human instead:

```bash
./scripts/vnc-display.sh
```

Connect a VNC viewer to the port it prints, log in through the browser window
that appears, and close the viewer. The session is saved and reused.

---

## When Instagram redesigns

It will happen. This is the maintenance routine, and it takes minutes:

```bash
# 1. See what the page looks like now
.venv/bin/python tools/probe_live.py --out probe

# 2. Compare probe/outline.json against igbot/selectors.yaml
#    and fix the changed selectors.

# 3. Re-run the checks
.venv/bin/python -m igbot doctor --deep
```

`igbot/selectors.yaml` holds every selector that could break, with the
findings that are easy to get wrong written next to them.

---

## How it works (for the curious)

- **Login** is by cookie. No password is ever handled by the bot.
- **Reading** works on the inbox rows, and uses Instagram's own `You:` prefix
  to tell your messages apart from everyone else's. It also listens to the
  GraphQL responses the page already fetches, so it adds no network traffic
  of its own.
- **Direction detection** — whether a bubble is yours or the customer's — is
  decided by three independent signals (bubble colour, structure, and
  position), so a theme change or a long message still classifies correctly.
- **Sending** is only ever counted as successful when a **new outgoing bubble
  actually appears in the thread**. Instagram empties the composer even when
  a message is rejected, so "the box is empty" is not treated as proof. If the
  send cannot be confirmed, the bot records it as unconfirmed and tells you —
  it never blindly retries, because a duplicate reply is worse than a missed
  one.
- **State** lives in `state/igbot.db` (SQLite), so restarts never cause a
  message to be answered twice.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No session cookie found` | Run `igbot login --cookie-file ...` |
| `Instagram did not accept that cookie` | The cookie expired. Get a fresh one. |
| `no Primary tab found` | Instagram changed the layout — see the maintenance section |
| `reply could NOT be confirmed` | Check the thread by hand; the bot did not retry on purpose |
| Bot never replies | `baseline_only` is still `true` in `config.yaml` |
| Everything is slow on a server | Use `scripts/igbot-xvfb.sh` — the browser needs a display |
| Login asks for 2FA | Log in by hand once via `scripts/vnc-display.sh` |

---

## Security notes

- `secrets/cookies.txt` **is** your account. Anyone with that file is you.
  It is stored with `600` permissions and is excluded from git.
- If you ever pasted a cookie into a chat, a support ticket or a shared
  document, **treat it as burned**: log out of Instagram everywhere and log
  back in to get a fresh one.
- The bot never asks for, stores, or reads your Instagram password.

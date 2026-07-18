# visaMonitor — US visa appointment monitor

Watches the live US visa appointment availability grid on
[qmq.app](https://qmq.app) and sends a **phone push** the moment an interview
date on or before your target date appears at any consulate you care about.
Runs itself on **GitHub Actions** (free, in the cloud, works while your computer
is off), with an optional local watch mode for ~60s reaction time.

You choose the visa type, sub-category, consulates and cutoff — see
**[Configuration](#configuration)**. All of it lives in repo Variables/Secrets,
never in this repo, so a public fork discloses nothing about *your* search.

Why a browser and not a simple `curl`: qmq.app renders its grid client-side over
a Supabase-Realtime (Phoenix) websocket — the raw HTML is empty and there is no
JSON API — so the monitor renders the page headless with Chromium and reads the
DOM.

### What an alert looks like

A per-location summary (earliest date + count, soonest location first) — not a
wall of dates. It lists **every** location with a qualifying date, and is
repeated 6× so you don't miss it:

```
F-1 Graduate · 3 locations by YYYY-MM-DD
北京: earliest 2026-09-21, 4 dates
广州: earliest 2026-10-13, 3 dates
沈阳: earliest 2026-10-15, 2 dates
```

(Tapping the notification opens qmq.app.)

### On-demand status check

Alerts only fire when a date meets your cutoff. To ask "what's the earliest slot
right now?" at any time, request a **status** — it reports the earliest date at
*every* location you watch, ignoring the cutoff, and marks with ✓ any that
already qualify. It's a single, non-urgent push and never touches the alert
dedup state, so it can't suppress a real alert.

There are two **mutually exclusive** views, both honouring your per-city
category rules (`VISA_DESC` + `VISA_DESC_BY_CITY`):

| Command | Reports |
|---------|---------|
| `status` | **regular** slots only — what a normal applicant can book |
| `emergency status` | **only** rows tagged `官方紧急申请 - 不是普通号` — the emergency pool |

Keeping them separate matters: an emergency slot is often much earlier than any
regular one, so mixing them would make a location look bookable when it isn't.

Three ways to trigger:

| From | How |
|------|-----|
| **Your phone** | Send `status` or `emergency status` as a message to your ntfy topic — the running watcher replies in ~1s |
| **GitHub** | Actions → *visaMonitor F-1* → **Run workflow** → mode `status` / `emergency-status` (works with your computer off) |
| **Terminal** | `python monitor.py --check` (add `--emergency` for the emergency-only view) |

```
F-1 status · none by 2026-12-31
广州: 2026-09-15, 48 dates (All Others, All Students, Graduate / PhD students)
武汉: 2026-10-09, 9 dates
北京: 2026-10-20, 19 dates

F-1 emergency status (官方紧急申请 only) · none by 2026-12-31
武汉: 2026-08-26, 1 date
广州: none
北京: none
```

> The phone trigger needs the **local watcher** running (only it holds an open
> connection to the topic). If your machine is off, use the GitHub trigger.

---

## Setup (about 5 minutes)

### 1. Get push notifications on your phone (ntfy — free, no account)

1. Install the **ntfy** app: [iOS](https://apps.apple.com/app/ntfy/id1625396347) · [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)
2. Generate a private, unguessable topic name and subscribe to it in the app
   (**+ Subscribe to topic**):

   ```bash
   echo "visamonitor-$(openssl rand -hex 8)"
   ```

   > ⚠️ **Treat this like a password.** ntfy topics are unauthenticated: anyone
   > who learns yours can read every alert you receive *and* push fake ones to
   > your phone. Never commit it — keep it in a GitHub **secret** and in your
   > local (uncommitted) launchd plist only. If it ever leaks, generate a new
   > one and re-subscribe.
3. Test it from a terminal — your phone should buzz:

   ```bash
   curl -d "hello from visaMonitor" ntfy.sh/YOUR_TOPIC
   ```

### 2. Put this folder on GitHub as a **public** repo

Keep it public so Actions minutes stay free (a 5-minute cron far exceeds the
private-repo free budget). No secrets live in the code — only in step 3.

```bash
cd ~/PythonProjects/visaMonitor
git init && git add . && git commit -m "visaMonitor F-1"
gh repo create visaMonitor --public --source=. --push   # or create it in the GitHub UI
```

### 3. Add your push topic as a repo secret

Repo → **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|------|-------|
| `VISA_NTFY_TOPIC` | `YOUR_TOPIC` |

### 4. Turn it on and test

1. Repo → **Actions** tab → enable workflows if prompted.
2. Open **“visaMonitor F-1” → Run workflow** (manual trigger).
3. Open the run’s log for the **Check availability** step. You want to see:

   ```
   STATUS: OK (parsed 5/5 cities)
   F-1 [Graduate]: no date by YYYY-MM-DD. Earliest: 广州=…; 北京=…; 上海=…; 沈阳=…; 武汉=…
   ```

   - `STATUS: OK` → the scrape works from GitHub’s servers. You’re done; it now
     runs every ~5 min automatically.
   - `STATUS: SCRAPE_FAILED (cloudflare)` → GitHub’s IP got blocked by
     Cloudflare. See **Troubleshooting** below.

That’s it. When a matching date opens, your phone buzzes with the
dates and a tap-through to qmq.app.

---

## Configuration

Set these as repo **Variables** (Settings → Secrets and variables → Actions →
**Variables**), *not* in the workflow file — that keeps your search private even
though the repo is public. The workflow reads them via `${{ vars.* }}`; anything
you leave unset falls back to a neutral default.

```bash
gh variable set VISA_CITIES --body "广州,北京"      # etc., one per setting
```

| Variable | Meaning | Example |
|----------|---------|---------|
| `VISA_CITIES` | Consulate cities to watch, comma-separated, Chinese names. Only cities on the default view load (the 5 China cities); others need their country tab, not yet automated. | `广州,北京,上海,沈阳,武汉` |
| `VISA_TYPE` | Visa badge prefix (matches all sub-categories) | `F-1`, `B1/B2`, `H-1B` |
| `VISA_DESC` | Default: narrow to one sub-category by description text (case-insensitive substring); empty = all | `Graduate` (Graduate/PhD), `All Students`, `All Others` |
| `VISA_DESC_BY_CITY` | **Per-city override** of `VISA_DESC`, as `城市=值` pairs. `ALL` = watch every sub-category in that city. | `上海=ALL` |
| `VISA_CUTOFF` | Alert on dates **on or before** this, inclusive (ISO) | `2026-12-31` |
| `VISA_INCLUDE_EMERGENCY` | Include "官方紧急申请 - 不是普通号" emergency-only slots (bookable only by users in the emergency pool). `0` = ignore (default), `1` = include | `0` |
| `VISA_PUSH_REPEAT` | Repeat each new alert this many times (numbered `i/N`) so you don't miss it | `6` |
| `VISA_PUSH_INTERVAL` | Seconds between those repeats | `5` |

> **Emergency slots:** some rows are tagged `官方紧急申请 - 不是普通号` — these are
> only bookable by applicants in the emergency-request pool, so they're excluded
> by default. When you're eligible, set `VISA_INCLUDE_EMERGENCY=1` (in the plist or
> workflow) to start alerting on them too; matched emergency dates are flagged
> `⚠emergency-only` in the logs.

Changing a Variable takes effect on the next scheduled run — no commit needed.

## Run it locally (optional)

```bash
pip install -r requirements.txt
python -m playwright install chromium
VISA_NTFY_TOPIC=YOUR_TOPIC python monitor.py --once   # one check
python monitor.py --test-push                                # send a test push
```

## Faster: near-real-time watch mode

The GitHub Actions cron checks every ~5 min. For a faster reaction, run the
built-in **watch loop** on an always-on machine (your Mac, or a small VPS). It
keeps one browser open and re-checks every ~60s:

```bash
VISA_NTFY_TOPIC=YOUR_TOPIC python monitor.py --watch 60   # every ~60s
```

Notes and trade-offs:
- **Why it reloads each cycle:** qmq.app fetches its grid once at load and does
  **not** live-update the page (verified: zero DOM changes while open). So true
  websocket-style "push the instant it appears" isn't available from the public
  site without reverse-engineering their private realtime protocol. Reloading
  every ~60s is the robust equivalent and reacts within a minute.
- **Needs an always-on process** — unlike the GitHub cron, it dies if the
  machine sleeps/closes. Run it under `caffeinate`, `tmux`, `nohup`, or a
  launchd/systemd service.
- **Be gentle:** don't go below ~30–60s. qmq is behind Cloudflare (Error 1015
  rate-limiting); aggressive reloads from one IP can get you temporarily
  blocked. The loop adds small random jitter and survives transient blocks.

Run it *and* the GitHub cron if you like — dedup state keeps them from
double-pinging (use separate state files / machines; they don't share state).

### Run the watcher 24/7 on your Mac (launchd)

A launchd agent ([`launchd/com.visamonitor.f1.plist`](launchd/com.visamonitor.f1.plist))
runs the watcher under `caffeinate`, starts it at login, and restarts it if it
dies.

The committed file is a **template** with `__PYTHON__` / `__PROJECT__` /
`__TOPIC__` placeholders, so your real paths and push topic are never committed.
Install substitutes them into `~/Library/LaunchAgents/` (outside the repo):

```bash
TOPIC="your-private-topic"                 # same value as your VISA_NTFY_TOPIC secret
PROJECT="$(pwd)"                           # run this from your clone
PYTHON="$(command -v python3)"             # must have playwright installed

sed -e "s#__PYTHON__#$PYTHON#g" \
    -e "s#__PROJECT__#$PROJECT#g" \
    -e "s#__TOPIC__#$TOPIC#g" \
    launchd/com.visamonitor.f1.plist > ~/Library/LaunchAgents/com.visamonitor.f1.plist

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.visamonitor.f1.plist
launchctl enable  gui/$(id -u)/com.visamonitor.f1
```

Manage it:

```bash
# status (look for "state = running" and a pid)
launchctl print gui/$(id -u)/com.visamonitor.f1 | grep -E 'state|pid'

# live logs
tail -f watch.log            # checks + alerts
tail -f watch.err.log        # errors

# restart after editing the INSTALLED plist (e.g. change interval / cutoff).
# Edit ~/Library/LaunchAgents/... directly -- do NOT copy the template over it,
# that would overwrite your real paths/topic with placeholders.
launchctl bootout   gui/$(id -u)/com.visamonitor.f1
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.visamonitor.f1.plist

# stop + uninstall completely
launchctl bootout gui/$(id -u)/com.visamonitor.f1
rm ~/Library/LaunchAgents/com.visamonitor.f1.plist
```

**Change what it watches:** edit the `EnvironmentVariables` (city / visa / desc /
cutoff / topic) or the `--watch 60` interval in
`~/Library/LaunchAgents/com.visamonitor.f1.plist`, then run the restart block above.

**Caveats:**
- It's a **login agent** — it starts when you're logged in, not at a pre-login
  boot screen. `caffeinate -i` blocks *idle* sleep, but **closing the lid on
  battery still sleeps** the Mac (macOS limitation); it resumes on wake.
- To edit config you change the installed plist under `~/Library/LaunchAgents/`
  (the copy in the repo is just the template).

## Pause / stop

Repo → **Actions → visaMonitor F-1 → ⋯ → Disable workflow**. Re-enable anytime.
(GitHub also auto-pauses scheduled workflows after 60 days with no repo commits.)

---

## Honest limitations

- **Free monitor, not an auto-grabber.** qmq’s paid service reacts in *seconds*.
  This tells you a slot *appeared* — it may already be taken by the time you tap
  through. It shortens your reaction time; it doesn’t book for you.
- **Timing drift.** GitHub delays scheduled runs under load; real cadence can be
  5–15 min, not a strict 5.
- **Cloudflare.** If datacenter IPs get blocked, see Troubleshooting.

## Troubleshooting

- **`SCRAPE_FAILED (cloudflare)` every run** — GitHub’s shared IPs are blocked.
  Options: (a) run it locally instead (a launchd/cron job on your Mac uses your
  home IP), or (b) route Chromium through a residential proxy. Ask and this can
  be wired in.
- **`SCRAPE_FAILED (timeout)` occasionally** — a slow render; harmless, the next
  run retries. If it’s *every* run, the site layout may have changed.
- **`EXTRACT_FAILED`** — the city name or F-1 rows weren’t found; the site’s
  markup likely changed and the selector in `monitor.py` needs a tweak.
- **No push but logs show a match** — check the `VISA_NTFY_TOPIC` secret is set
  and your phone is subscribed to that exact topic.

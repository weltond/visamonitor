# visaMonitor — US visa appointment monitor

Monitors the public **recent successful grabs** feed on [qmq.app](https://qmq.app).
When a new record matches your consulate, visa type and date cutoff, GitHub Actions
can send a phone notification through Bark, ntfy, or both. Chromium renders the
feed cards. You can use GitHub Actions and Bark's public server without a VPS or
US visa scheduling credentials.

### What an alert looks like

Bark sends one notification per new batch. Each consulate and visa type shows its
earliest date and, when applicable, how many dates were found in the batch.

```
上海美签发现新号源！
签证类型：B1/B2
领事馆：上海
可预约日期：2027-01-15
请尽快进入官网查看并预约。日期以官网为准。
```

Tapping the Bark notification opens [US Visa Scheduling](https://www.usvisascheduling.com/).
The date comes from a third-party release signal. Check the official site for
current availability. ntfy keeps its date list, click-through to qmq.app, repeat
count and optional e-mail copy.

### On-demand status check

Status shows recent grab dates for each watched location, including dates past
the cutoff, and marks qualifying locations with ✓. It sends one push per
selected channel and leaves the alert history alone. `emergency-status` and
`--emergency` remain aliases for status because the public feed has no emergency
slot tag.

| From | How |
|------|-----|
| Your phone | Send `status` to your ntfy topic while the local watcher is running with ntfy enabled |
| GitHub | Actions → **visaMonitor F-1** → **Run workflow** → **status** |
| Terminal | `python3 monitor.py --check` |

## Set up Bark for Shanghai B1/B2

1. Install [Bark](https://github.com/Finb/Bark) on your iPhone and copy the device
   key from the app's push setup. In **Settings → Notifications → Bark**, allow
   notifications, Lock Screen alerts and Time Sensitive Notifications. If
   testing in a Focus mode, allow time-sensitive notifications in that Focus
   as well.
2. In your GitHub repository, open **Settings → Secrets and variables →
   Actions → Secrets**. Create a repository secret named `VISA_BARK_KEY` with
   the device key alone. Keep it out of source files and workflow Variables.
3. In the **Variables** tab, set the example values below. Set the cutoff to
   your target date. The last two rows are optional because they match the defaults.

   | Variable | Example |
   |----------|---------|
   | `VISA_CITIES` | `上海` |
   | `VISA_TYPE` | `B1/B2` |
   | `VISA_CUTOFF` | `2027-01-31` |
   | `VISA_NOTIFY_CHANNELS` | `bark` |
   | `VISA_BARK_SERVER` | `https://api.day.app` |
   | `VISA_BARK_LEVEL` | `timeSensitive` |

4. Commit and push the changes to your repository's default branch. In **Actions**,
   enable workflows if prompted. Lock your iPhone, then use **visaMonitor F-1 →
   Run workflow → test-push** from another device. Look for `[bark] push accepted`
   in **Check availability**, verify the test message appears on the lock screen,
   and tap it to confirm the official site opens.
5. Run the workflow again in **monitor** mode and look for `STATUS: OK`. A fresh
   cache records a baseline. Subsequent runs alert on new matching records.
   The existing five-minute schedule stays in place. GitHub may delay runs.

`test-push` uses the selected channels and does not scrape qmq.app or touch the
monitor state. It exits with an error if any selected channel fails. Bark API
acceptance and iPhone notification display are separate checks.

## Set up ntfy (optional)

Install [ntfy for iOS](https://apps.apple.com/app/ntfy/id1625396347) or
[Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy).
Generate a private topic with `echo "visamonitor-$(openssl rand -hex 8)"`,
subscribe to that topic in the app and set the repository secret
`VISA_NTFY_TOPIC` to its value. Anyone who knows an unprotected topic can read
or send its messages, so treat the name as a secret.

If `VISA_NOTIFY_CHANNELS` is unset or empty, ntfy is used as before. Choose
`bark,ntfy` to send to both. Each channel runs independently. To have ntfy forward
release alerts by e-mail, optionally set the repository secrets `VISA_EMAIL` and
`VISA_NTFY_TOKEN` (an access token from [your ntfy account](https://ntfy.sh/account)).
Only the first ntfy copy requests e-mail. If it fails, the copy is retried
without e-mail forwarding. The [token setup script](scripts/set-ntfy-token.sh)
is available for the optional local launchd watcher.

## Configuration

| Environment variable | Purpose and default |
|----------------------|---------------------|
| `VISA_NOTIFY_CHANNELS` | `ntfy` (default), `bark`, or `bark,ntfy` |
| `VISA_BARK_KEY` | Bark device key. Required for Bark. Use a GitHub Secret. |
| `VISA_BARK_SERVER` | Server base URL, without the device key or `/push`. Default `https://api.day.app`. Use HTTPS for remote servers. |
| `VISA_BARK_LEVEL` | `timeSensitive` (default), `active`, `passive` or `critical` |
| `VISA_CITIES` | Comma-separated consulate names as shown in the feed. Default `广州,北京,上海,沈阳,武汉`. |
| `VISA_TYPE` | Visa badge prefix, case-sensitive. Default `H-1B`. |
| `VISA_CUTOFF` | Last qualifying interview date (inclusive, ISO `YYYY-MM-DD`). Default 60 days ahead. |
| `VISA_PUSH_REPEAT` | Number of ntfy copies per release alert (default 6). |
| `VISA_PUSH_INTERVAL` | Seconds between ntfy copies (default 5). |
| `VISA_BURST_MINUTES` | Minutes of the hour to poll faster (default `0,30`, local `--watch` only). |
| `VISA_BURST_WINDOW` | Seconds on either side of a burst minute (default 60, local only). |
| `VISA_BURST_INTERVAL` | Seconds between checks during a burst (default 10, local only). |

The public feed supplies city, visa badge, date and record age. The older grid
settings `VISA_DESC`, `VISA_DESC_BY_CITY`, `VISA_INCLUDE_EMERGENCY` and
`VISA_EMERGENCY_TYPES` do not affect the current monitor. The original grid
implementation is archived in `monitor_v1_grid_backup.py`.

## Run it locally (optional)

A real Bark push test requires Python 3 and a Bark device key. Run this from
the repository root. It reads the key without echoing it or adding it to shell
history. Lock the iPhone before pressing Enter.

```bash
bash -c '
  read -rsp "Bark device key: " VISA_BARK_KEY && [ -n "$VISA_BARK_KEY" ] || exit 1
  printf "\n"
  export VISA_BARK_KEY
  VISA_NOTIFY_CHANNELS=bark VISA_BARK_SERVER=https://api.day.app \
    VISA_BARK_LEVEL=timeSensitive VISA_CITIES=上海 VISA_TYPE=B1/B2 \
    VISA_CUTOFF=2027-01-31 python3 monitor.py --test-push
'
```

For a local feed check, install the browser dependencies and set your search
and notification environment variables before running:

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
python3 monitor.py --once
```

Run the automated tests with `python3 -m unittest discover -s tests -v`. They
mock both notification providers and do not send a device notification.

## Faster: near-real-time watch mode

The GitHub Actions cron checks every ~5 min. For a faster reaction, run the
built-in **watch loop** on an always-on machine (your Mac, or a small VPS). It
keeps one browser open and re-checks every ~60s:

```bash
VISA_NTFY_TOPIC=YOUR_TOPIC python monitor.py --watch 60   # every ~60s
```

Notes and trade-offs:
- **Feed refresh:** the watcher reloads qmq.app and extracts the public feed on
  each check.
- **Needs an always-on process** — unlike the GitHub cron, it dies if the
  machine sleeps/closes. Run it under `caffeinate`, `tmux`, `nohup`, or a
  launchd/systemd service.
- **Burst polling:** consulates release slots on the hour and half hour, and a
  released slot can be taken within seconds — a flat 60s poll samples those
  critical moments at random. So within `VISA_BURST_WINDOW` seconds of each
  `VISA_BURST_MINUTES` mark the loop switches to `VISA_BURST_INTERVAL`
  (default: ~10 checks across `:29–:31` and `:59–:01`, vs ~2 before), then
  returns to the normal interval. Minutes-of-the-hour are timezone-independent
  for whole-hour offsets, so `:30` local is `:30` at the consulate.
- **Be gentle:** don't go below ~30–60s outside burst windows. qmq is behind Cloudflare (Error 1015
  rate-limiting); aggressive reloads from one IP can get you temporarily
  blocked. The loop adds small random jitter and survives transient blocks.

The local watcher and GitHub Actions each have their own state. Running both
against the same feed can produce two alerts for one release signal.

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

**Change what it watches:** edit the `EnvironmentVariables` (city / visa /
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

- **Release signals.** The feed records dates other users grabbed. Availability
  can change before you open the official site.
- **Timing drift.** GitHub delays scheduled runs under load; real cadence can be
  5–15 min, not a strict 5.
- **Deduplication.** Once a city, visa badge and date enter `state.json`, the same
  record stays suppressed even if it disappears from the feed and later returns.
  Dates more than 180 days in the past are pruned. The Actions cache carries
  state across runs. A missing cache starts a new baseline.
- **Delivery failures.** Bark makes up to three attempts for transient failures.
  The monitor then records the feed record as seen, so a failed channel is not
  replayed on later runs. If a request timed out after the server accepted it, a
  retry may result in a duplicate. Check the channel logs for delivery errors.
- **Cloudflare.** If datacenter IPs get blocked, see Troubleshooting.

## Troubleshooting

- **`SCRAPE_FAILED (cloudflare)` every run** — GitHub’s shared IPs are blocked.
  Options: (a) run it locally instead (a launchd/cron job on your Mac uses your
  home IP), or (b) route Chromium through a residential proxy. Ask and this can
  be wired in.
- **`SCRAPE_FAILED (timeout)` occasionally** — a slow render; harmless, the next
  run retries. If it’s *every* run, the site layout may have changed.
- **`EXTRACT_FAILED`** — the monitor could not read feed cards. Check whether the
  site layout changed.
- **No notification** — check `VISA_NOTIFY_CHANNELS` and run `test-push`. For
  Bark, confirm the device key, base URL and iOS notification settings. For
  ntfy, confirm `VISA_NTFY_TOPIC` and the corresponding app subscription.

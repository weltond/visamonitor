#!/usr/bin/env python3
"""
visaMonitor -- US visa appointment release-signal monitor.

qmq.app 改版后的适配版（原"城市网格"已下线，见 2026-05-16 站方公告）：
  * 原来的"N 个可用日期"城市网格如今被一次人机验证挡住，自动化无法通过；
  * 首页顶部的「最近抢位成功记录」feed 是公开的、无需验证，数据为
    （城市, 签证类型, 日期, 距今）四元组，且分钟级更新。

因此本版监控该 feed：当某个 watched 城市 + 签证类型出现**新的**成功抢位记录、
且其日期早于等于截止日 VISA_CUTOFF 时，通过 ntfy.sh 推送提醒。语义说明：
一条新记录意味着该领馆刚放出一批号（已有用户抢到一个），放号通常是成批的，
看到推送后应立即去官方 CGI 系统查看并预约。

Config is via environment variables (all prefixed VISA_, legacy QMQ_ accepted).
`python3 monitor.py --once` = single check (scheduler); `--watch` = keep-one-
browser real-time loop; `--check` = on-demand status push.
"""

import argparse
import datetime as dt
import json
import os
import queue
import random
import re
import signal
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------- #
# Config (override with env vars, all prefixed VISA_)
# --------------------------------------------------------------------------- #
def _env(name: str, default: str = "", legacy: str = "") -> str:
    """Read VISA_<name>, falling back to the legacy QMQ_<legacy or name>.

    An env var that is set but EMPTY counts as unset -- GitHub Actions renders
    an undefined `vars.X` as "", which would otherwise blow away the default.
    """
    for key in (f"VISA_{name}", f"QMQ_{legacy or name}"):
        val = os.environ.get(key)
        if val not in (None, ""):
            return val
    return default


# Consulate cities to watch (Chinese names as they appear in the feed),
# comma-separated. The feed is global; anything not listed here is ignored.
_cities = _env("CITIES") or os.environ.get("QMQ_CITY", "") or "广州,北京,上海,沈阳,武汉"
CITIES = [c.strip() for c in _cities.split(",") if c.strip()]
VISA_PREFIX = _env("TYPE", "H-1B", legacy="VISA")    # matches any badge starting with this
# Alert on dates ON OR BEFORE this (inclusive, ISO yyyy-mm-dd).
CUTOFF = _env("CUTOFF") or (dt.date.today() + dt.timedelta(days=60)).isoformat()
# The feed's freshest record being older than this many minutes triggers a
# one-time "source looks frozen" push (and a recovery push later). NOTE: the
# feed only gains rows when someone actually grabs a slot, so quiet stretches
# are normal -- this is a conservative "is the pipe alive" check, not a
# freshness guarantee. Default 12h.
STALE_AFTER_MIN = float(_env("STALE_AFTER_MIN", "720") or 720)
# When a NEW matching record appears, repeat the push this many times, this
# many seconds apart, so you don't overlook it.
PUSH_REPEAT = max(1, int(_env("PUSH_REPEAT", "6") or 6))
PUSH_INTERVAL = max(0, int(_env("PUSH_INTERVAL", "5") or 5))
# Slots are released on the hour and half hour, so a fixed 60s poll samples the
# critical seconds at random. Around each mark we poll much faster.
# NOTE: these are minutes-of-the-hour, identical in any whole-hour timezone.
BURST_MARKS = [int(x) for x in _env("BURST_MINUTES", "0,30").split(",") if x.strip().isdigit()]
BURST_WINDOW = max(0, int(_env("BURST_WINDOW", "60") or 60))       # seconds either side of a mark
BURST_INTERVAL = max(2, int(_env("BURST_INTERVAL", "10") or 10))   # seconds between checks inside it
NTFY_TOPIC = _env("NTFY_TOPIC")                      # ntfy.sh topic to publish to (REQUIRED for push)
NTFY_SERVER = _env("NTFY_SERVER", "https://ntfy.sh")
# Optional second channel: ntfy forwards a copy of an ALERT to this address.
# ntfy.sh rejects anonymous email sending ("code 40053"), so NTFY_TOKEN must
# hold an access token from a (free) ntfy account for this to work.
EMAIL_TO = _env("EMAIL")
NTFY_TOKEN = _env("NTFY_TOKEN")
STATE_FILE = Path(_env("STATE") or Path(__file__).with_name("state.json"))
# Append-only audit trail of every check (see check_and_notify).
HISTORY_FILE = Path(_env("HISTORY") or Path(__file__).with_name("history.jsonl"))
_today = _env("TODAY")
TODAY = dt.date.fromisoformat(_today) if _today else dt.date.today()
URL = "https://qmq.app"

# --------------------------------------------------------------------------- #
# DOM extraction -- runs inside the rendered page. The public feed lives in a
# marquee of small cards (Tailwind), each shaped like:
#   <div class="... bg-card ...">
#     <div><span class="h-1.5 w-1.5 rounded-full bg-primary"></span><span>香港</span></div>
#     <span>H-1B</span><span>2026-10-21</span><span><svg…/>2天</span>
#   </div>
# We key on div[class*="bg-card"] with exactly 4 children and an ISO date in
# child #3 -- resilient to class reshuffling elsewhere on the page.
# The marquee duplicates its content to loop seamlessly; dedup happens in Python.
# --------------------------------------------------------------------------- #
EXTRACT_JS = r"""
() => {
  const ISO = /^\d{4}-\d{2}-\d{2}$/;
  const out = [];
  for (const card of document.querySelectorAll("div[class*='bg-card']")) {
    const kids = Array.from(card.children);
    if (kids.length !== 4) continue;
    const date = kids[2].textContent.trim();
    if (!ISO.test(date)) continue;
    const cityEl = kids[0].querySelector("span:last-child");
    out.push({
      city: cityEl ? cityEl.textContent.trim() : "",
      visa: kids[1].textContent.trim(),
      date: date,
      age: kids[3].textContent.trim(),
    });
  }
  return { records: out };
}
"""

WAIT_FEED_JS = (
    "() => Array.from(document.querySelectorAll(\"div[class*='bg-card']\"))"
    ".some(c => /\\d{4}-\\d{2}-\\d{2}/.test(c.innerText))"
)

CF_MARKERS = ("Just a moment", "cf-browser-verification", "Attention Required",
              "Error 1015", "rate limited", "Checking your browser")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _new_page(browser):
    """A fresh context+page with a realistic locale/UA."""
    ctx = browser.new_context(locale="zh-CN", user_agent=UA)
    return ctx.new_page()


def launch_browser(p, headless: bool = True):
    """Launch Playwright's bundled Chromium; fall back to the system Chrome
    channel (needed on macOS 11, where Playwright ships no chromium build)."""
    args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
    try:
        return p.chromium.launch(headless=headless, args=args)
    except Exception as e:
        reason = str(e) or e.__class__.__name__
        print(f"[launch] bundled chromium unavailable ({reason[:80]}); using system Chrome", flush=True)
        return p.chromium.launch(headless=headless, channel="chrome", args=args)


def render_and_extract(page) -> dict:
    """(Re)load qmq.app on an existing page and return the public release feed.

    Raises RuntimeError('cloudflare') on a Cloudflare challenge/rate limit,
    and RuntimeError('timeout') if the feed never renders. Reusable across many
    reloads (watch mode) so we keep one browser process open.
    """
    from playwright.sync_api import TimeoutError as PWTimeout
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
        head = (page.content()[:4000] if page.content() else "")
        if any(m in head for m in CF_MARKERS):
            raise RuntimeError("cloudflare")
        # Wait for the public feed cards (no human verification needed for these).
        page.wait_for_function(WAIT_FEED_JS, timeout=45_000)
        return page.evaluate(EXTRACT_JS)
    except PWTimeout:
        body = (page.content()[:4000] if page.content() else "")
        if any(m in body for m in CF_MARKERS):
            raise RuntimeError("cloudflare")
        raise RuntimeError("timeout")


def scrape() -> dict:
    """One-shot: launch a browser, render+extract once, close."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = launch_browser(p, headless=True)
        try:
            return render_and_extract(_new_page(browser))
        finally:
            browser.close()


# --------------------------------------------------------------------------- #
# Record handling
# --------------------------------------------------------------------------- #
def parse_age_minutes(txt: str):
    """'刚刚'->0, '39分钟'->39, '15小时'->900, '2天'->2880.

    This is how long ago someone grabbed that slot. The feed only grows when
    grabs happen, so old entries are normal (quiet period), not staleness.
    """
    if not txt:
        return None
    if "刚刚" in txt:
        return 0.0
    mm = re.search(r"(\d+)\s*(秒|分钟|小时|天|周)前?", txt)
    if not mm:
        return None
    return int(mm.group(1)) * {"秒": 1 / 60, "分钟": 1, "小时": 60,
                               "天": 1440, "周": 10080}[mm.group(2)]


def _wanted(record: dict) -> bool:
    """True when a feed record matches the watched cities + visa prefix."""
    if record.get("city") not in CITIES:
        return False
    return bool(record.get("visa", "").startswith(VISA_PREFIX))


def dedup_records(data: dict) -> list:
    """The marquee repeats itself for seamless scrolling -- drop duplicates."""
    seen, out = set(), []
    for r in data.get("records", []):
        key = f"{r.get('city')}|{r.get('visa')}|{r.get('date')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def find_matches(records: list, cutoff: dt.date) -> list:
    """Watched cities, watched visa, date on/before cutoff -> release signals."""
    matches = []
    for r in records:
        if not _wanted(r):
            continue
        try:
            d = dt.date.fromisoformat(r["date"])
        except (KeyError, ValueError):
            continue
        if d <= cutoff:
            matches.append({"city": r["city"], "visa": r["visa"],
                            "date": r["date"], "age": r.get("age", "")})
    matches.sort(key=lambda m: (m["date"], m["city"]))
    return matches


def target_label() -> str:
    return f"{VISA_PREFIX} [{','.join(CITIES)}]"


# --------------------------------------------------------------------------- #
# State -- v2, keyed to the feed. A pre-revision state.json (v1, grid-era) is
# discarded: on the first run after upgrading we silently capture a baseline
# instead of flooding the user with alerts for days-old records.
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            s = json.loads(STATE_FILE.read_text())
            if s.get("fmt") == 2:
                return s
        except Exception:
            pass
    return {"fmt": 2, "fresh": False, "known": {}, "src_stale": False}


def save_state(state: dict) -> None:
    state["fmt"] = 2
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def prune_known(state: dict) -> None:
    """Drop known dates older than 180 days so the state can't grow forever."""
    horizon = (TODAY - dt.timedelta(days=180)).isoformat()
    for key in list(state["known"].keys()):
        state["known"][key] = [d for d in state["known"][key] if d >= horizon]
        if not state["known"][key]:
            del state["known"][key]


# --------------------------------------------------------------------------- #
# Push (ntfy.sh)
# --------------------------------------------------------------------------- #
def push(title: str, body: str, click: str = URL, priority: str = "urgent",
         email: str = "") -> None:
    if not NTFY_TOPIC:
        print("[warn] VISA_NTFY_TOPIC not set -- skipping push. Message was:\n", title, body)
        return
    headers = {
        "Title": title.encode("utf-8").decode("latin-1", "ignore"),
        "Priority": priority,
        "Click": click,
    }
    if email:
        headers["Email"] = email          # ntfy forwards a copy by e-mail
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{urllib.parse.quote(NTFY_TOPIC)}",
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def push_repeated(title: str, body: str, click: str = URL) -> None:
    """Send the same alert PUSH_REPEAT times, PUSH_INTERVAL seconds apart, so an
    alert is hard to overlook. Each copy is numbered (i/N). Failures don't abort."""
    if not NTFY_TOPIC:
        push(title, body, click)  # prints the "not set" warning once
        return
    for i in range(PUSH_REPEAT):
        suffix = f" ({i + 1}/{PUSH_REPEAT})" if PUSH_REPEAT > 1 else ""
        # Only the FIRST copy carries the e-mail header -- one e-mail per alert.
        email = EMAIL_TO if i == 0 else ""
        try:
            push(title + suffix, body, click, email=email)
        except Exception as e:
            print(f"[push] copy {i + 1}/{PUSH_REPEAT} failed: {e}", flush=True)
            if email:
                print("[push] retrying copy 1 without e-mail forwarding", flush=True)
                try:
                    push(title + suffix, body, click)
                except Exception as e2:
                    print(f"[push] retry also failed: {e2}", flush=True)
        if i < PUSH_REPEAT - 1:
            time.sleep(PUSH_INTERVAL)


# --------------------------------------------------------------------------- #
# Core check
# --------------------------------------------------------------------------- #
def check_and_notify(data: dict, verbose: bool = True) -> None:
    """Given extracted data: log status, and push on genuinely NEW qualifying
    records (city+visa+date not alerted before)."""
    cutoff = dt.date.fromisoformat(CUTOFF)
    stamp = dt.datetime.now().isoformat(timespec="seconds")

    if data.get("error"):
        print(f"[{stamp}] STATUS: EXTRACT_FAILED ({data['error']}) -- "
              f"feed cards not found (site layout may have changed)", flush=True)
        return

    records = dedup_records(data)
    ages = [parse_age_minutes(r.get("age", "")) for r in records]
    ages = [a for a in ages if a is not None]
    freshest = min(ages) if ages else None
    if freshest is None:
        age_note = ""
    elif freshest >= STALE_AFTER_MIN:
        age_note = f"; feed STALE (freshest record {freshest / 60:.1f}h old)"
    else:
        age_note = f"; freshest record {freshest:.0f}m"
    print(f"[{stamp}] STATUS: OK (feed {len(records)} records; "
          f"watching {target_label()} by {CUTOFF}{age_note})", flush=True)

    matches = find_matches(records, cutoff)
    if verbose:
        if matches:
            print(f"[{stamp}] {target_label()} release signals by {cutoff}:")
            for m in matches:
                print(f"    {m['city']} {m['visa']} -> {m['date']} ({m['age']} ago)")
        else:
            earliest = {}
            for r in records:
                if not _wanted(r):
                    continue
                d = r.get("date")
                if not d:
                    continue
                cur = earliest.get(r["city"])
                if cur is None or d < cur:
                    earliest[r["city"]] = d
            line = "; ".join(f"{c}={earliest.get(c, '—')}" for c in CITIES if c in earliest)
            print(f"[{stamp}] {target_label()}: no release signal by {cutoff}. "
                  f"Earliest grab records: {line}")

    state = load_state()
    known = state["known"]

    # A record is NEW if we have never alerted this city+visa+date combination.
    # "known" is cumulative (not a snapshot): feed rows rotate out of the
    # marquee, and a rotated-out date reappearing means a brand-new grab there.
    new_hits = [m for m in matches
                if m["date"] not in known.get(f"{m['city']}|{m['visa']}", [])]

    if not state.get("fresh"):
        # First run with the v2 format (or after a state reset): capture a
        # baseline silently instead of pushing alerts for every old record.
        print(f"[{stamp}] baseline captured ({len(matches)} existing qualifying records) "
              f"-- alerts start from the next check", flush=True)
        state["fresh"] = True
    elif new_hits:
        # Group by location: city -> [(visa, date, age)], earliest date first.
        by_city: dict = {}
        for m in new_hits:
            by_city.setdefault(m["city"], []).append(m)
        lines = []
        for c in sorted(by_city, key=lambda c: min(x["date"] for x in by_city[c])):
            items = sorted(by_city[c], key=lambda x: x["date"])
            parts = ", ".join(f"{x['visa']} {x['date']}"
                              + (f" ({x['age']} ago)" if x["age"] else "") for x in items)
            lines.append(f"{c}: {parts}")
        n = len(new_hits)
        title = f"‼️ {VISA_PREFIX} 放号信号 · {n} 条 by {CUTOFF}"
        push_repeated(title, "\n".join(lines))
        print(f"[{stamp}] PUSHED x{PUSH_REPEAT} (every {PUSH_INTERVAL}s): "
              + " | ".join(lines), flush=True)

    # Merge the current qualifying set into the cumulative known map.
    for m in matches:
        known.setdefault(f"{m['city']}|{m['visa']}", [])
        if m["date"] not in known[f"{m['city']}|{m['visa']}"]:
            known[f"{m['city']}|{m['visa']}"].append(m["date"])
    prune_known(state)

    # Tell the user when the feed pipe recovers / freezes. While the source is
    # frozen we cannot see new grabs at all, so recovery is itself news.
    was_stale = bool(state.get("src_stale"))
    is_stale = freshest is not None and freshest >= STALE_AFTER_MIN
    if was_stale and not is_stale:
        push(f"{VISA_PREFIX} source is LIVE again",
             f"qmq feed is fresh again (freshest record {freshest:.0f}m old).\n"
             f"Release monitoring is effective from now on.", priority="high")
        print(f"[{stamp}] SOURCE RECOVERED (age {freshest:.0f}m) -- notified", flush=True)
    elif is_stale and not was_stale:
        push(f"{VISA_PREFIX} source looks frozen",
             f"No new grab records for {freshest / 60:.1f}h. Quiet stretch or "
             f"upstream issue -- keep an eye out.", priority="default")
        print(f"[{stamp}] SOURCE WENT STALE (age {freshest:.0f}m) -- notified", flush=True)
    state["src_stale"] = is_stale

    state["last_checked"] = stamp
    save_state(state)

    # Durable audit trail: one compact line per check. watch.log can be rotated
    # or truncated; this lets you reconstruct "what did we see at time T?".
    try:
        line = json.dumps({"t": stamp, "matched": matches, "freshest_age_min": freshest,
                           "seen": records}, ensure_ascii=False)
        with HISTORY_FILE.open("a") as fh:
            fh.write(line + "\n")
    except Exception:
        pass  # auditing must never break monitoring


def parse_command(text: str):
    """Map an inbound ntfy message to a status request.

    "status" / "emergency status" both map to a plain status now: the public
    feed no longer splits regular vs. 官方紧急申请 pools (that tag lived in the
    removed grid). The two spellings are kept so old phone commands still work.
    """
    t = " ".join((text or "").strip().lower().split())
    if t in ("status", "check", "s", "emergency status", "status emergency",
             "emergency", "es"):
        return "status"
    return None


# Commands arrive on a background thread but must be executed on the main thread:
# Playwright's sync API is not thread-safe, so the listener only enqueues.
COMMANDS: "queue.Queue" = queue.Queue()


def listen_for_commands() -> None:
    """Background thread: watch our own ntfy topic and enqueue status commands.

    ntfy streams the topic as newline-delimited JSON. We only react to plain
    messages with NO title -- every push we send has a title, so the bot can
    never answer itself and loop.
    """
    if not NTFY_TOPIC:
        return
    # No `since=` -- that default means "stream messages from now on".
    url = f"{NTFY_SERVER}/{urllib.parse.quote(NTFY_TOPIC)}/json"
    while True:
        try:
            with urllib.request.urlopen(url) as stream:
                for raw in stream:
                    try:
                        msg = json.loads(raw.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if msg.get("event") != "message" or msg.get("title"):
                        continue  # keepalive/open event, or one of our own pushes
                    mode = parse_command(msg.get("message", ""))
                    if mode is None:
                        continue
                    stamp = dt.datetime.now().isoformat(timespec="seconds")
                    print(f"[{stamp}] COMMAND: {msg.get('message', '').strip()!r} -> {mode}", flush=True)
                    COMMANDS.put(mode)
        except Exception:
            pass  # network blip / stream closed -- reconnect after a pause
        time.sleep(5)


def build_status(records: list) -> tuple:
    """On-demand status: matching grab records per watched city, earliest date
    first; dates at/below the cutoff are marked ✓."""
    cutoff = dt.date.fromisoformat(CUTOFF)
    by_city: dict = {}
    for r in records:
        if not _wanted(r):
            continue
        by_city.setdefault(r["city"], []).append(r)
    lines = []
    for city in CITIES:
        items = sorted(by_city.get(city, []), key=lambda r: r["date"])
        if not items:
            lines.append((None, f"{city}: no records"))
            continue
        earliest = items[0]["date"]
        detail = ", ".join(f"{r['visa']} {r['date']} ({r['age']})" for r in items[:4])
        extra = f" +{len(items) - 4}" if len(items) > 4 else ""
        mark = " ✓" if dt.date.fromisoformat(earliest) <= cutoff else ""
        lines.append((dt.date.fromisoformat(earliest),
                      f"{city}: {detail}{extra}{mark}"))
    lines.sort(key=lambda t: (t[0] is None, t[0] or dt.date.max))
    hits = sum(1 for d, _ in lines if d and d <= cutoff)
    title = (f"‼️ {VISA_PREFIX} records · {hits} at/before {CUTOFF}" if hits
             else f"{VISA_PREFIX} records · none by {CUTOFF}")
    return title, "\n".join(text for _, text in lines)


def run_check(page=None) -> int:
    """On-demand: push a snapshot of matching records per watched city.

    `page` lets the watch loop reuse its open browser instead of launching a
    second Chromium just to answer a command.
    """
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    try:
        data = render_and_extract(page) if page is not None else scrape()
    except Exception as e:
        reason = str(e) or e.__class__.__name__
        print(f"[{stamp}] STATUS: SCRAPE_FAILED ({reason})", flush=True)
        push(f"{VISA_PREFIX} status unavailable", f"Could not read qmq.app: {reason}",
             priority="default")
        return 0
    if data.get("error"):
        print(f"[{stamp}] STATUS: EXTRACT_FAILED ({data['error']})", flush=True)
        push(f"{VISA_PREFIX} status unavailable", f"Extract failed: {data['error']}",
             priority="default")
        return 0
    title, body = build_status(dedup_records(data))
    print(f"[{stamp}] {title}\n" + "\n".join("    " + b for b in body.split("\n")), flush=True)
    # single, non-urgent push: this is user-requested, not a slot alert. Does not
    # touch dedup state, so it can never suppress a real alert.
    push(title, body, priority="default")
    return 0


def run_once() -> int:
    try:
        data = scrape()
    except Exception as e:
        # Soft-fail: keep a scheduled run "green" so we don't spam failure emails
        # on a transient Cloudflare block / timeout. A persistent block shows in logs.
        stamp = dt.datetime.now().isoformat(timespec="seconds")
        reason = str(e) or e.__class__.__name__
        print(f"[{stamp}] STATUS: SCRAPE_FAILED ({reason}) -- site unreadable this run", flush=True)
        return 0
    check_and_notify(data)
    return 0


def in_burst_window(now: dt.datetime) -> bool:
    """True when within BURST_WINDOW seconds of a slot-release mark (:00/:30).

    Distance is measured around the hour, so :59:30 correctly counts as near
    the :00 mark.
    """
    if not BURST_MARKS:
        return False
    secs = now.minute * 60 + now.second
    for mark in BURST_MARKS:
        diff = abs(secs - mark * 60)
        if min(diff, 3600 - diff) <= BURST_WINDOW:
            return True
    return False


def run_watch(interval: int, recycle: int = 40) -> int:
    """Keep ONE browser open and re-check every ~`interval` seconds (near real-time).

    Each cycle reloads the page. A transient Cloudflare block / timeout just
    skips that cycle. Needs an always-on machine (your Mac or a small VPS).
    Ctrl-C to stop.
    """
    from playwright.sync_api import sync_playwright
    # Treat SIGTERM (how launchd/systemd stop us) like Ctrl-C so `finally`
    # runs and Chromium is closed cleanly instead of orphaned.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    # Answer "status" / "emergency status" messages sent to our ntfy topic.
    threading.Thread(target=listen_for_commands, daemon=True).start()
    print(f"[watch] every ~{interval}s: {target_label()} by {CUTOFF}. Ctrl-C to stop.", flush=True)
    with sync_playwright() as p:
        browser = launch_browser(p, headless=True)
        page = _new_page(browser)
        n = 0
        try:
            was_hot = False
            while True:
                n += 1
                started = time.monotonic()
                try:
                    check_and_notify(render_and_extract(page))
                except Exception as e:
                    stamp = dt.datetime.now().isoformat(timespec="seconds")
                    reason = str(e) or e.__class__.__name__
                    print(f"[{stamp}] STATUS: SCRAPE_FAILED ({reason})", flush=True)
                # Periodically recycle the browser context to bound memory.
                if n % recycle == 0:
                    try:
                        page.context.close()
                    except Exception:
                        pass
                    page = _new_page(browser)
                # Near a release mark, poll fast; otherwise use the normal
                # interval (with jitter so we aren't perfectly periodic).
                hot = in_burst_window(dt.datetime.now())
                if hot != was_hot:
                    print(f"[watch] {'ENTERING' if hot else 'leaving'} burst mode "
                          f"({BURST_INTERVAL}s cadence)", flush=True)
                    was_hot = hot
                gap = BURST_INTERVAL if hot else interval + random.uniform(0, min(15, interval * 0.25))
                # Measure from the START of the check so a slow render eats into
                # the wait rather than stretching the cadence.
                deadline = started + gap
                # Idle until then, but stay responsive to commands: poll the
                # queue and answer on THIS thread (Playwright isn't thread-safe).
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    try:
                        mode = COMMANDS.get(timeout=min(1.0, left))
                    except queue.Empty:
                        continue
                    try:
                        run_check(page=page)
                    except Exception as e:
                        print(f"[command] failed: {e}", flush=True)
        except KeyboardInterrupt:
            print("\n[watch] stopped.", flush=True)
        finally:
            browser.close()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="visaMonitor -- US visa release-signal monitor")
    ap.add_argument("--once", action="store_true", help="check a single time and exit (default)")
    ap.add_argument("--watch", nargs="?", type=int, const=60, metavar="SECONDS",
                    help="stay running, re-check every SECONDS (default 60, min 20); "
                         "near real-time, needs an always-on machine")
    ap.add_argument("--check", action="store_true",
                    help="on-demand: push matching grab records per watched city and exit")
    ap.add_argument("--emergency", action="store_true",
                    help="kept for scheduler compatibility; the public feed no longer "
                         "distinguishes the 官方紧急申请 pool, so this has no effect")
    ap.add_argument("--test-push", action="store_true", help="send a test push and exit")
    args = ap.parse_args()

    if args.test_push:
        push("visaMonitor test",
             f"Watching {target_label()} for release signals by {CUTOFF}.")
        print("test push sent (if VISA_NTFY_TOPIC set)")
        return

    if args.check:
        sys.exit(run_check())

    if args.watch is not None:
        sys.exit(run_watch(max(20, args.watch)))

    sys.exit(run_once())


if __name__ == "__main__":
    main()

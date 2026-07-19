#!/usr/bin/env python3
"""
visaMonitor -- US visa appointment availability monitor.

Watches the live availability grid on https://qmq.app for given cities + visa
type and sends a phone push (via ntfy.sh) when an appointment date on/before a
cutoff appears. Remembers what it already alerted so you are not notified twice
for the same date.

The grid on qmq.app is rendered client-side over a Supabase Realtime (Phoenix)
websocket -- there is no JSON API and the raw HTML is empty -- so we render the
page with a headless browser and read the DOM.

Config is via environment variables (see CONFIG below) with sensible defaults.
Run `python3 monitor.py --once` to check a single time (used by the scheduler).
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

    An env var that is set but EMPTY counts as unset -- GitHub Actions renders an
    undefined `vars.X` as "", which would otherwise blow away the default (an
    empty VISA_TYPE, for instance, would match every visa type).
    """
    for key in (f"VISA_{name}", f"QMQ_{legacy or name}"):
        val = os.environ.get(key)
        if val not in (None, ""):
            return val
    return default


# Consulate cities to watch (Chinese names as shown on the site), comma-separated.
# Default = the 5 China cities, which all load on the default view.
_cities = _env("CITIES") or os.environ.get("QMQ_CITY", "") or "广州,北京,上海,沈阳,武汉"
CITIES = [c.strip() for c in _cities.split(",") if c.strip()]
VISA_PREFIX = _env("TYPE", "F-1", legacy="VISA")     # matches any row whose badge starts with this (F-1 = all F-1 subcategories)
DESC_CONTAINS = _env("DESC").strip()                 # narrow by row description substring, case-insensitive; "" (default) = all sub-categories
# Per-city override of that filter: "城市=值" pairs, comma-separated.
# Value "ALL" (or empty) means: no narrowing for that city -- watch every
# sub-category of VISA_TYPE there, while every other city keeps whatever
# VISA_DESC says. e.g. "上海=ALL" or "上海=All Students,北京=Graduate".
DESC_BY_CITY: dict[str, str] = {}
for _pair in _env("DESC_BY_CITY").split(","):
    if "=" in _pair:
        _c, _, _d = _pair.partition("=")
        _d = _d.strip()
        DESC_BY_CITY[_c.strip()] = "" if _d.upper() in ("ALL", "*", "") else _d
# Alert on dates ON OR BEFORE this (inclusive, ISO yyyy-mm-dd).
# No meaningful default is possible, so fall back to "60 days out" -- set
# VISA_CUTOFF to your real target date.
CUTOFF = _env("CUTOFF") or (dt.date.today() + dt.timedelta(days=60)).isoformat()
# "官方紧急申请 - 不是普通号" rows are emergency-only slots (not bookable by regular
# applicants). Excluded by default; set VISA_INCLUDE_EMERGENCY=1 to include them.
INCLUDE_EMERGENCY = _env("INCLUDE_EMERGENCY").strip().lower() in ("1", "true", "yes", "on")
# Visa badges that share the SAME emergency (官方紧急申请) slot pool. At these
# consulates F-1 and J-1 draw on one pool, so the emergency view must look at
# both or it will under-report. Used only by the "emergency status" view.
EMERGENCY_TYPES = [t.strip() for t in _env("EMERGENCY_TYPES", "F-1,J-1").split(",") if t.strip()]
# When a NEW matching date appears, repeat the push this many times, this many
# seconds apart, so you don't overlook it.
PUSH_REPEAT = max(1, int(_env("PUSH_REPEAT", "6") or 6))
PUSH_INTERVAL = max(0, int(_env("PUSH_INTERVAL", "5") or 5))
NTFY_TOPIC = _env("NTFY_TOPIC")                      # ntfy.sh topic to publish to (REQUIRED for push)
NTFY_SERVER = _env("NTFY_SERVER", "https://ntfy.sh")
# Optional second channel: ntfy forwards a copy of an ALERT to this address.
# ntfy.sh rejects anonymous email sending ("code 40053"), so NTFY_TOKEN must
# hold an access token from a (free) ntfy account for this to work.
EMAIL_TO = _env("EMAIL")
NTFY_TOKEN = _env("NTFY_TOKEN")
STATE_FILE = Path(_env("STATE") or Path(__file__).with_name("state.json"))
_today = _env("TODAY")
TODAY = dt.date.fromisoformat(_today) if _today else dt.date.today()
URL = "https://qmq.app"

# --------------------------------------------------------------------------- #
# DOM extraction -- runs inside the rendered page, returns ALL visible city
# cards as JSON: { cities: [{ city, count, rows:[{badge,desc,status,dates}] }] }.
# The grid's innerText is a flat sequence of city sections; each begins with a
# "<city>" name line followed by an "<N> 个可用日期" count line, then visa rows:
#   [badge, description, status(有位/紧缺), "N前更新", date-chips(月日 + weekday)...]
# Chips are sorted earliest-first, so the earliest date shows even when collapsed.
# --------------------------------------------------------------------------- #
EXTRACT_JS = r"""
() => {
  const btns = Array.from(document.querySelectorAll('button'));
  const cityBtns = btns.filter(b => /\d[\d,]*\s*个可用日期/.test(b.textContent));
  if (!cityBtns.length) return { error: 'no-city-cards' };
  // Walk up to the smallest container that holds every city card.
  const markers = el => (el.innerText.match(/个可用日期/g) || []).length;
  let box = cityBtns[0];
  while (box.parentElement && markers(box) < cityBtns.length) box = box.parentElement;
  const lines = box.innerText.split('\n').map(s => s.trim()).filter(Boolean);

  const BADGE = /^(B1|B1\/B2|B2|F-1|F-2|H-1B|H-4|J-1|L-1|L-2|K-1|O-1|C1\/D|F1)$/;
  const DESCK = /(Visa|Student|Crew|Waiver|Others|specialty|professionals|Exchange)/i;
  const COUNT = /^(\D*?)(\d[\d,]*)\s*个可用日期$/;
  const EMERG = /官方紧急申请|不是普通号/;  // emergency-only slot tag

  // City sections start at each "count" line; city name is on that line or the one before.
  const secs = [];
  for (let i = 0; i < lines.length; i++) {
    const m = lines[i].match(COUNT);
    if (m) {
      const city = (m[1].trim()) || (lines[i - 1] || '?').trim();
      secs.push({ city, count: m[2], start: i + 1 });
    }
  }
  const parseRows = seg => {
    const heads = [];
    for (let i = 0; i < seg.length - 1; i++) {
      if (BADGE.test(seg[i]) && DESCK.test(seg[i + 1] || '')) heads.push({ i, badge: seg[i], desc: seg[i + 1] });
    }
    return heads.map((h, k) => {
      const end = k + 1 < heads.length ? heads[k + 1].i : seg.length;
      const part = seg.slice(h.i, end);
      const status = part.find(s => s === '有位' || s === '紧缺') || '?';
      const emergency = part.some(s => EMERG.test(s));
      const dates = part.filter(s => /^\d{1,2}月\d{1,2}日$/.test(s));
      return { badge: h.badge, desc: h.desc, status, emergency, dates };
    });
  };
  const cities = secs.map((s, k) => {
    const end = k + 1 < secs.length ? secs[k + 1].start - 1 : lines.length;  // drop next city's name line
    return { city: s.city, count: s.count, rows: parseRows(lines.slice(s.start, end)) };
  });
  return { cities };
}
"""


def parse_cn_date(chip: str, today: dt.date) -> dt.date:
    """'8月11日' -> date. Year inferred: if month < today's month, it's next year."""
    m = re.match(r"(\d{1,2})月(\d{1,2})日", chip)
    if not m:
        raise ValueError(chip)
    month, day = int(m.group(1)), int(m.group(2))
    year = today.year if month >= today.month else today.year + 1
    return dt.date(year, month, day)


CF_MARKERS = ("Just a moment", "cf-browser-verification", "Attention Required",
              "Error 1015", "rate limited", "Checking your browser")


UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _new_page(browser):
    """A fresh context+page with a realistic locale/UA."""
    ctx = browser.new_context(locale="zh-CN", user_agent=UA)
    return ctx.new_page()


def render_and_extract(page, today: dt.date) -> dict:
    """(Re)load qmq.app on an existing page and return the extracted city rows.

    Raises RuntimeError('cloudflare') on a Cloudflare challenge/rate limit,
    and RuntimeError('timeout') if the grid never renders. Reusable across many
    reloads (watch mode) so we keep one browser process open.
    """
    from playwright.sync_api import TimeoutError as PWTimeout
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
        head = (page.content()[:4000] if page.content() else "")
        if any(m in head for m in CF_MARKERS):
            raise RuntimeError("cloudflare")
        # Wait for the grid to populate: a city card shows its date count.
        page.wait_for_function(
            "() => Array.from(document.querySelectorAll('button'))"
            ".some(b => /\\d[\\d,]*\\s*个可用日期/.test(b.textContent))",
            timeout=45_000,
        )
        page.wait_for_function(
            "() => document.body.innerText.includes('F-1')", timeout=20_000
        )
        return page.evaluate(EXTRACT_JS)
    except PWTimeout:
        body = (page.content()[:4000] if page.content() else "")
        if any(m in body for m in CF_MARKERS):
            raise RuntimeError("cloudflare")
        raise RuntimeError("timeout")


def scrape(today: dt.date) -> dict:
    """One-shot: launch a browser, render+extract once, close."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            return render_and_extract(_new_page(browser), today)
        finally:
            browser.close()


def _desc_filter(city: str) -> str:
    """Description filter for a city: per-city override, else the global default."""
    return DESC_BY_CITY.get(city, DESC_CONTAINS)


# How to treat rows tagged 官方紧急申请 - 不是普通号 ("emergency-only" slots):
# Prefixed to a title whenever a date on/before the cutoff exists.
ALERT_MARK = "‼️"

EMG_EXCLUDE = "exclude"   # regular slots only  -> the "status" view and normal alerts
EMG_ONLY = "only"         # emergency slots only -> the "emergency status" view
EMG_INCLUDE = "include"   # both


def _row_wanted(row: dict, city: str = "", emergency: str | None = None) -> bool:
    """`emergency=None` uses the configured default (VISA_INCLUDE_EMERGENCY);
    pass EMG_EXCLUDE / EMG_ONLY / EMG_INCLUDE to override for one request.

    The emergency pool is allocated differently from regular slots, so the
    EMG_ONLY view deliberately filters differently -- see EMERGENCY_TYPES.
    """
    mode = emergency or (EMG_INCLUDE if INCLUDE_EMERGENCY else EMG_EXCLUDE)
    is_emergency = bool(row.get("emergency"))

    if mode == EMG_ONLY:
        # Emergency slots are pooled: F-1 and J-1 draw on the SAME slots, and the
        # F-1 sub-categories are not distinguished either. So widen to
        # EMERGENCY_TYPES and ignore the per-city description narrowing entirely
        # -- otherwise we would hide slots that are in fact available.
        return is_emergency and any(row["badge"].startswith(t) for t in EMERGENCY_TYPES)

    if not row["badge"].startswith(VISA_PREFIX):
        return False
    desc = _desc_filter(city) if city else DESC_CONTAINS
    if desc and desc.lower() not in row["desc"].lower():
        return False
    if mode == EMG_EXCLUDE and is_emergency:
        return False   # emergency-only slot, not bookable by regular applicants
    return True


def _short_desc(desc: str) -> str:
    """'F-1 Student • Students - Graduate / PhD students' -> 'Graduate / PhD students'."""
    tail = desc.split("•")[-1].strip()
    return tail.replace("Students - ", "").strip() or desc


def target_label() -> str:
    """Human-readable description of what we're watching, incl. per-city rules."""
    parts = [VISA_PREFIX, f"[{DESC_CONTAINS or 'ALL'}]"]
    if DESC_BY_CITY:
        parts.append("(" + ", ".join(f"{c}={d or 'ALL'}" for c, d in DESC_BY_CITY.items()) + ")")
    return " ".join(parts)


def _cities_index(data: dict) -> dict:
    return {c["city"]: c for c in data.get("cities", [])}


def find_matches(data: dict, today: dt.date, cutoff: dt.date) -> list[dict]:
    """Across the wanted CITIES, return matching rows (>=1 date on/before cutoff)."""
    matches = []
    idx = _cities_index(data)
    for city in CITIES:
        c = idx.get(city)
        if not c:
            continue
        for row in c["rows"]:
            if not _row_wanted(row, city):
                continue
            hits = []
            for chip in row["dates"]:
                try:
                    d = parse_cn_date(chip, today)
                except ValueError:
                    continue
                if d <= cutoff:
                    hits.append(d.isoformat())
            if hits:
                matches.append({"city": city, "badge": row["badge"], "desc": row["desc"],
                                "status": row["status"], "emergency": row.get("emergency", False),
                                "dates": sorted(set(hits))})
    return matches


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


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
        # Only the FIRST copy carries the e-mail header -- one e-mail per alert,
        # not one per repeat.
        email = EMAIL_TO if i == 0 else ""
        try:
            push(title + suffix, body, click, email=email)
        except Exception as e:
            print(f"[push] copy {i + 1}/{PUSH_REPEAT} failed: {e}", flush=True)
            if email:
                # E-mail forwarding can fail on its own (no ntfy account/token,
                # quota). It must never take the actual alert down with it.
                print("[push] retrying copy 1 without e-mail forwarding", flush=True)
                try:
                    push(title + suffix, body, click)
                except Exception as e2:
                    print(f"[push] retry also failed: {e2}", flush=True)
        if i < PUSH_REPEAT - 1:
            time.sleep(PUSH_INTERVAL)


def check_and_notify(data: dict, verbose: bool = True) -> None:
    """Given extracted data: log status, and push on genuinely NEW qualifying dates."""
    cutoff = dt.date.fromisoformat(CUTOFF)
    stamp = dt.datetime.now().isoformat(timespec="seconds")

    if data.get("error"):
        print(f"[{stamp}] STATUS: EXTRACT_FAILED ({data['error']}) -- "
              f"no city cards / F-1 rows found (site layout may have changed)", flush=True)
        return

    idx = _cities_index(data)
    found = [c for c in CITIES if c in idx]
    missing = [c for c in CITIES if c not in idx]
    note = f"parsed {len(found)}/{len(CITIES)} cities" + (f"; MISSING {','.join(missing)}" if missing else "")
    print(f"[{stamp}] STATUS: OK ({note})", flush=True)

    matches = find_matches(data, TODAY, cutoff)
    # Fingerprint keyed by city+category so alerts are per-city and dedup correctly.
    fp = {f"{m['city']} · {m['desc']}": m["dates"] for m in matches}

    state = load_state()
    prev = state.get("last_matches", {})

    # New qualifying dates = anything present now that wasn't alerted before.
    new_hits = {}
    for key, dates in fp.items():
        fresh = [d for d in dates if d not in prev.get(key, [])]
        if fresh:
            new_hits[key] = fresh

    target = target_label()
    if verbose:
        if matches:
            print(f"[{stamp}] {target} MATCHES by {cutoff}:")
            for m in matches:
                emg = " ⚠emergency-only" if m.get("emergency") else ""
                print(f"    {m['city']} {m['badge']} ({m['desc']}) [{m['status']}]{emg}: {', '.join(m['dates'])}")
        else:
            earliest = []
            for city in CITIES:
                c = idx.get(city)
                if not c:
                    continue
                firsts = [row["dates"][0] for row in c["rows"] if _row_wanted(row, city) and row["dates"]]
                earliest.append(f"{city}={min(firsts, key=lambda s: parse_cn_date(s, TODAY)) if firsts else '—'}")
            print(f"[{stamp}] {target}: no date by {cutoff}. Earliest: {'; '.join(earliest)}")

    if new_hits:
        # Summarize matching availability per LOCATION: earliest date + count of
        # qualifying dates (not every date). Locations sorted earliest-first.
        by_city: dict[str, dict] = {}
        for m in matches:
            e = by_city.setdefault(m["city"], {"dates": set(), "descs": set()})
            e["dates"].update(m["dates"])
            e["descs"].add(m["desc"])
        summary = sorted(((c, sorted(e["dates"]), e["descs"]) for c, e in by_city.items()),
                         key=lambda t: t[1][0])
        lines = []
        for c, ds, descs in summary:
            plural = "" if len(ds) == 1 else "s"
            # Cities watched across several sub-categories (per-city override) also
            # name which category opened -- otherwise the alert is ambiguous.
            cats = ""
            if c in DESC_BY_CITY:
                cats = " (" + ", ".join(sorted(_short_desc(d) for d in descs)) + ")"
            lines.append(f"{c}: earliest {ds[0]}, {len(ds)} date{plural}{cats}")
        loc = f"{len(summary)} location{'' if len(summary) == 1 else 's'}"
        # An alert only fires because a date qualified, so it always leads with ‼️.
        title = f"{ALERT_MARK} {VISA_PREFIX} · {loc} by {CUTOFF}"
        body = "\n".join(lines)
        push_repeated(title, body)
        print(f"[{stamp}] PUSHED x{PUSH_REPEAT} (every {PUSH_INTERVAL}s): " + " | ".join(lines))

    # Persist current qualifying set so we only alert on genuinely new dates.
    state["last_matches"] = fp
    state["last_checked"] = stamp
    save_state(state)


def parse_command(text: str) -> str | None:
    """Map an inbound ntfy message to a status request.

    "status"           -> EMG_EXCLUDE: regular slots only
    "emergency status" -> EMG_ONLY:    官方紧急申请 - 不是普通号 slots only
    anything else      -> None (ignore)
    """
    t = " ".join((text or "").strip().lower().split())
    if t in ("status", "check", "s"):
        return EMG_EXCLUDE
    if t in ("emergency status", "status emergency", "emergency", "es"):
        return EMG_ONLY
    return None


# Commands arrive on a background thread but must be executed on the main thread:
# Playwright's sync API is not thread-safe, so the listener only enqueues.
COMMANDS: "queue.Queue[str]" = queue.Queue()


def listen_for_commands() -> None:
    """Background thread: watch our own ntfy topic and enqueue status commands.

    ntfy streams the topic as newline-delimited JSON. We only react to plain
    messages with NO title -- every push we send has a title, so the bot can
    never answer itself and loop.
    """
    if not NTFY_TOPIC:
        return
    # No `since=` -- that default means "stream messages from now on". (Passing
    # since=now is rejected with HTTP 400; valid values are a duration/id/all.)
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


def build_status(data: dict, emergency: str = EMG_EXCLUDE) -> tuple[str, str]:
    """On-demand status: the earliest slot at EACH watched location.

    Unlike the alert path this ignores the cutoff (so it still tells you
    something when nothing qualifies); dates at/below the cutoff are marked ✓.
    `emergency=EMG_ONLY` reports ONLY 官方紧急申请 - 不是普通号 rows; the default
    reports only regular ones. The per-city category rules apply either way.
    """
    cutoff = dt.date.fromisoformat(CUTOFF)
    idx = _cities_index(data)
    lines: list[tuple[dt.date | None, str]] = []
    for city in CITIES:
        c = idx.get(city)
        if not c:
            lines.append((None, f"{city}: not found"))
            continue
        dates, labels = set(), set()
        for row in c["rows"]:
            if not _row_wanted(row, city, emergency):
                continue
            for chip in row["dates"]:
                try:
                    dates.add(parse_cn_date(chip, TODAY))
                except ValueError:
                    continue
            if row["dates"]:
                # Emergency view spans visa types (F-1/J-1), so name the badge;
                # otherwise name the sub-category, but only where a city is
                # broadened (elsewhere it's always the same one).
                labels.add(row["badge"] if emergency == EMG_ONLY
                           else _short_desc(row["desc"]) if city in DESC_BY_CITY else "")
        if not dates:
            lines.append((None, f"{city}: none"))
            continue
        earliest = min(dates)
        named = sorted(x for x in labels if x)
        cats = f" ({', '.join(named)})" if named else ""
        mark = " ✓" if earliest <= cutoff else ""
        plural = "" if len(dates) == 1 else "s"
        lines.append((earliest, f"{city}: {earliest.isoformat()}, {len(dates)} date{plural}{cats}{mark}"))
    # soonest first; locations with nothing sink to the bottom
    lines.sort(key=lambda t: (t[0] is None, t[0] or dt.date.max))
    hits = sum(1 for d, _ in lines if d and d <= cutoff)
    # The emergency view spans the pooled visa types, so name them, not VISA_TYPE.
    if emergency == EMG_ONLY:
        what = f"{'/'.join(EMERGENCY_TYPES)} emergency status (官方紧急申请 only)"
    elif emergency == EMG_INCLUDE:
        what = f"{VISA_PREFIX} status (regular + emergency)"
    else:
        what = f"{VISA_PREFIX} status"
    # Lead with ‼️ whenever something is actually on/before the cutoff, so a
    # hit is obvious from the notification title alone.
    title = (f"{ALERT_MARK} {what} · {hits} at/before {CUTOFF}" if hits
             else f"{what} · none by {CUTOFF}")
    return title, "\n".join(text for _, text in lines)


def run_check(emergency: str = EMG_EXCLUDE, page=None) -> int:
    """On-demand: push a snapshot of the earliest slot per location.

    `page` lets the watch loop reuse its open browser instead of launching a
    second Chromium just to answer a command.
    """
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    try:
        data = render_and_extract(page, TODAY) if page is not None else scrape(TODAY)
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
    title, body = build_status(data, emergency)
    print(f"[{stamp}] {title}\n" + "\n".join("    " + b for b in body.split("\n")), flush=True)
    # single, non-urgent push: this is user-requested, not a slot alert. Does not
    # touch dedup state, so it can never suppress a real alert.
    push(title, body, priority="default")
    return 0


def run_once() -> int:
    try:
        data = scrape(TODAY)
    except Exception as e:
        # Soft-fail: keep a scheduled run "green" so we don't spam failure emails
        # on a transient Cloudflare block / timeout. A persistent block shows in logs.
        stamp = dt.datetime.now().isoformat(timespec="seconds")
        reason = str(e) or e.__class__.__name__
        print(f"[{stamp}] STATUS: SCRAPE_FAILED ({reason}) -- site unreadable this run", flush=True)
        return 0
    check_and_notify(data)
    return 0


def run_watch(interval: int, recycle: int = 40) -> int:
    """Keep ONE browser open and re-check every ~`interval` seconds (near real-time).

    The site does not live-update its DOM, so each cycle reloads the page. A
    transient Cloudflare block / timeout just skips that cycle. Needs an
    always-on machine (your Mac or a small VPS). Ctrl-C to stop.
    """
    from playwright.sync_api import sync_playwright
    # Treat SIGTERM (how launchd/systemd stop us) like Ctrl-C so `finally`
    # runs and Chromium is closed cleanly instead of orphaned.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    # Answer "status" / "emergency status" messages sent to our ntfy topic.
    threading.Thread(target=listen_for_commands, daemon=True).start()
    print(f"[watch] every ~{interval}s: {'/'.join(CITIES)} {target_label()} by {CUTOFF}. "
          f"Ctrl-C to stop.", flush=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = _new_page(browser)
        n = 0
        try:
            while True:
                n += 1
                try:
                    check_and_notify(render_and_extract(page, TODAY))
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
                # Idle until the next cycle, but stay responsive to commands:
                # poll the queue every second and answer on THIS thread.
                deadline = time.monotonic() + interval + random.uniform(0, min(15, interval * 0.25))
                while time.monotonic() < deadline:
                    try:
                        mode = COMMANDS.get(timeout=1.0)
                    except queue.Empty:
                        continue
                    try:
                        run_check(emergency=mode, page=page)
                    except Exception as e:
                        print(f"[command] failed: {e}", flush=True)
        except KeyboardInterrupt:
            print("\n[watch] stopped.", flush=True)
        finally:
            browser.close()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="visaMonitor -- US visa appointment availability monitor")
    ap.add_argument("--once", action="store_true", help="check a single time and exit (default)")
    ap.add_argument("--watch", nargs="?", type=int, const=60, metavar="SECONDS",
                    help="stay running, re-check every SECONDS (default 60, min 20); "
                         "near real-time, needs an always-on machine")
    ap.add_argument("--check", action="store_true",
                    help="on-demand: push the earliest slot at each watched location "
                         "(ignores the cutoff) and exit")
    ap.add_argument("--emergency", action="store_true",
                    help="with --check: report ONLY 官方紧急申请 - 不是普通号 slots "
                         "(default reports only regular ones)")
    ap.add_argument("--test-push", action="store_true", help="send a test push and exit")
    args = ap.parse_args()

    if args.test_push:
        push("visaMonitor test",
             f"Monitoring {VISA_PREFIX} [{DESC_CONTAINS}] in {'/'.join(CITIES)} for dates by {CUTOFF}.")
        print("test push sent (if VISA_NTFY_TOPIC set)")
        return

    if args.check:
        sys.exit(run_check(emergency=EMG_ONLY if args.emergency else EMG_EXCLUDE))

    if args.watch is not None:
        sys.exit(run_watch(max(20, args.watch)))

    sys.exit(run_once())


if __name__ == "__main__":
    main()

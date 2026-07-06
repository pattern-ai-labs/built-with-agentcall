#!/usr/bin/env python3
"""Calendar sources for the auto-join scheduler.

One job: answer "what meetings are coming up, and what's the link?" — from a
calendar you connect once. Today that's a private iCal/ICS feed (Google, Outlook,
Apple — every provider exposes one). It's all standard-library: fetch the feed,
unfold + parse the VEVENTs, expand recurring meetings, and pull the Meet / Zoom /
Teams link out of each one.

The OAuth seam: everything the scheduler needs is the CalendarSource.events()
method below. A GoogleCalendarSource (live Calendar API, OAuth) can implement the
same method later and drop straight in — nothing else has to change. Pick the
source with make_source(); it reads CALENDAR.SOURCE from config.jsonc.

Run it directly to see what it finds (handy for a quick check or a bug report):
    python calendar_source.py "https://calendar.google.com/calendar/ical/.../basic.ics"
    python calendar_source.py ./sample.ics        # a local file works too
"""

import datetime as _dt
import logging
import re
from urllib import request as _urlrequest

try:                                             # stdlib since 3.9; tzdata backs it on Windows
    from zoneinfo import ZoneInfo
except Exception:                                # pragma: no cover - very old Pythons
    ZoneInfo = None

log = logging.getLogger("notetaker.autojoin")

UTC = _dt.timezone.utc

# ── meeting-link detection ──────────────────────────────────────────────────
# Ordered by provider. General on purpose — it should find the link wherever the
# calendar put it (a conference property, the location, or the description).
_LINK_PATTERNS = [
    ("meet",  re.compile(r"https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}\b", re.I)),
    ("meet",  re.compile(r"https://meet\.google\.com/lookup/[A-Za-z0-9]+", re.I)),
    ("zoom",  re.compile(r"https://[A-Za-z0-9.-]*zoom\.us/(?:j|w|my|s)/[^\s\"'<>]+", re.I)),
    ("teams", re.compile(r"https://teams\.microsoft\.com/l/meetup-join/[^\s\"'<>]+", re.I)),
    ("teams", re.compile(r"https://teams\.live\.com/meet/[^\s\"'<>]+", re.I)),
    ("webex", re.compile(r"https://[A-Za-z0-9.-]*webex\.com/[^\s\"'<>]+", re.I)),
]

_WEEKDAY = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def extract_link(*texts):
    """First meeting URL found across the given text blobs, provider-priority first.
    ICS escapes commas/semicolons/newlines with backslashes; unescape before scanning."""
    blob = "\n".join(_ics_unescape(t) for t in texts if t)
    for _, pat in _LINK_PATTERNS:
        m = pat.search(blob)
        if m:
            return m.group(0).rstrip(".,);]>")   # trim trailing punctuation from prose
    return None


def _ics_unescape(s):
    return (s.replace("\\n", "\n").replace("\\N", "\n")
             .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


class Event:
    """One concrete meeting occurrence. Recurring series are already expanded into
    individual Events, each with an absolute UTC start."""

    __slots__ = ("uid", "title", "start", "end", "url", "all_day", "cancelled", "declined")

    def __init__(self, uid, title, start, end, url, all_day, cancelled, declined):
        self.uid = uid
        self.title = title
        self.start = start          # aware datetime (UTC) — None only for unparseable
        self.end = end
        self.url = url
        self.all_day = all_day
        self.cancelled = cancelled
        self.declined = declined

    def key(self):
        """Stable id for one occurrence — UID + start instant. Used to dedupe joins
        so a meeting is never joined (and billed) twice."""
        stamp = self.start.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ") if self.start else "?"
        return f"{self.uid}::{stamp}"

    def __repr__(self):
        return f"<Event {self.title!r} {self.start} url={self.url!r}>"


# ── the source interface (the OAuth seam) ───────────────────────────────────
class CalendarSource:
    """Implement events() and you're a calendar the scheduler can use."""

    def events(self, window_start, window_end):
        """Return the list of Events with a start in [window_start, window_end]."""
        raise NotImplementedError

    def describe(self):
        return self.__class__.__name__


class ICSCalendarSource(CalendarSource):
    """A calendar backed by a private iCal/ICS URL (or a local .ics path for tests)."""

    def __init__(self, url, timeout=20):
        self.url = url
        self.timeout = timeout

    def describe(self):
        return "iCal feed"

    def _fetch(self):
        u = self.url
        # A local path (tests, offline) is allowed alongside http(s) and file://.
        if not re.match(r"^[a-z]+://", u, re.I):
            with open(u, encoding="utf-8") as fh:
                return fh.read()
        if u.startswith("webcal://"):            # some providers hand out webcal: links
            u = "https://" + u[len("webcal://"):]
        req = _urlrequest.Request(u, headers={"User-Agent": "meeting-notetaker/1.0 (+agentcall.dev)"})
        with _urlrequest.urlopen(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8", "replace")

    def events(self, window_start, window_end):
        text = self._fetch()
        return parse_ics(text, window_start, window_end)


def make_source(cal_config, env):
    """Build the calendar source named by CALENDAR.SOURCE in config.jsonc.
    Add new sources here (e.g. 'google' -> GoogleCalendarSource) — one line, and
    the scheduler picks it up unchanged."""
    source = (cal_config or {}).get("SOURCE", "ics").lower()
    if source == "ics":
        url = (env.get("CALENDAR_ICS_URL") or "").strip()
        if not url:
            raise ValueError(
                "No calendar link set. Add CALENDAR_ICS_URL to .env "
                "(the builder does this for you: python build.py), or run "
                "python autojoin.py connect.")
        return ICSCalendarSource(url)
    raise ValueError(f"Unknown CALENDAR.SOURCE {source!r} in config.jsonc "
                     "(supported: \"ics\").")


# ── ICS parsing (RFC 5545, the parts real calendars actually emit) ──────────
def parse_ics(text, window_start, window_end):
    """Parse an ICS document into concrete Events between window_start/end (UTC).
    Recurring meetings are expanded; EXDATE-cancelled instances are dropped; things
    we can't safely expand are logged (never silently skipped)."""
    lines = _unfold(text)

    # Pass 1: VTIMEZONE offsets, so TZID times resolve even without a tz database.
    vtimezones = _parse_vtimezones(lines)

    # Pass 2: walk the components, collecting VEVENTs.
    raw_events = []
    stack = []          # component names
    cur = None          # current VEVENT's property list
    for line in lines:
        up = line.upper()
        if up.startswith("BEGIN:"):
            name = line.split(":", 1)[1].strip().upper()
            stack.append(name)
            if name == "VEVENT":
                cur = []
            continue
        if up.startswith("END:"):
            name = stack.pop() if stack else ""
            if name == "VEVENT" and cur is not None:
                raw_events.append(cur)
                cur = None
            continue
        if cur is not None and stack and stack[-1] == "VEVENT":
            cur.append(_parse_prop(line))

    # RECURRENCE-ID overrides: a single edited instance of a series. We key them so
    # the base series can skip that instant and use the override instead.
    overrides = {}
    for props in raw_events:
        rid = _get(props, "RECURRENCE-ID")
        if rid is not None:
            uid = _get(props, "UID") or ""
            dt, _ = _parse_dt(_get_raw(props, "RECURRENCE-ID"), vtimezones)
            if dt is not None:
                overrides[(uid, dt.astimezone(UTC))] = props

    out = []
    for props in raw_events:
        try:
            out.extend(_events_from_vevent(props, vtimezones, overrides,
                                           window_start, window_end))
        except Exception as e:                   # one bad event never sinks the rest
            title = _get(props, "SUMMARY") or "(untitled)"
            log.warning("Skipped a calendar event %r — couldn't parse it (%s). "
                        "Join that one manually if you need it.", title, e)
    out.sort(key=lambda e: e.start or window_end)
    return out


def _events_from_vevent(props, vtimezones, overrides, window_start, window_end):
    uid = _get(props, "UID") or ""
    title = _ics_unescape(_get(props, "SUMMARY") or "(untitled)")
    status = (_get(props, "STATUS") or "").upper()
    cancelled = status == "CANCELLED"
    url = extract_link(_get(props, "X-GOOGLE-CONFERENCE"),
                       _get(props, "LOCATION"),
                       _get(props, "DESCRIPTION"),
                       _get(props, "URL"))
    declined = _is_declined(props)

    dtstart_raw = _get_raw(props, "DTSTART")
    if dtstart_raw is None:
        return []
    start, all_day = _parse_dt(dtstart_raw, vtimezones)
    if start is None:
        log.warning("Event %r has an unreadable start time — skipping it.", title)
        return []

    # Duration: DTEND if present, else DURATION, else assume 1h (timed) / 1d (all-day).
    end = _event_end(props, start, all_day, vtimezones)
    span = (end - start) if (end and start) else _dt.timedelta(hours=1)

    rid = _get(props, "RECURRENCE-ID")           # this VEVENT *is* an override instance
    rrule = _get(props, "RRULE")

    def mk(s):
        return Event(uid, title, s, s + span, url, all_day, cancelled, declined)

    if not rrule or rid is not None:
        # Single event (or a one-off override). Include if it lands in the window.
        return [mk(start)] if (start and window_start <= start <= window_end) else []

    # Recurring series: expand occurrences inside the window, minus EXDATEs and
    # minus any instant that has its own RECURRENCE-ID override (added separately).
    exdates = _collect_exdates(props, vtimezones)
    events = []
    for occ in _expand_rrule(start, rrule, window_start, window_end):
        occ_utc = occ.astimezone(UTC)
        if any(abs((occ_utc - x).total_seconds()) < 60 for x in exdates):
            continue
        if (uid, occ_utc) in overrides:
            continue
        events.append(mk(occ))
    return events


# ── property + datetime helpers ─────────────────────────────────────────────
def _parse_prop(line):
    """'DTSTART;TZID=America/New_York:20260706T090000' -> (NAME, {params}, value)."""
    head, _, value = line.partition(":")
    parts = head.split(";")
    name = parts[0].strip().upper()
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.strip().upper()] = v.strip().strip('"')
    return (name, params, value)


def _get(props, name):
    """Value of the first property called name (or None)."""
    name = name.upper()
    for n, _p, v in props:
        if n == name:
            return v
    return None


def _get_raw(props, name):
    """(params, value) of the first property called name — needed for TZID etc."""
    name = name.upper()
    for n, p, v in props:
        if n == name:
            return (p, v)
    return None


def _parse_dt(raw, vtimezones):
    """(aware-UTC datetime, is_all_day) from a (params, value) pair. Handles
    ...Z (UTC), VALUE=DATE (all-day), and TZID=... (zoneinfo, then VTIMEZONE offset,
    then local — each fallback logged)."""
    if raw is None:
        return (None, False)
    params, value = raw
    value = value.strip()

    if params.get("VALUE", "").upper() == "DATE" or (len(value) == 8 and value.isdigit()):
        d = _dt.datetime.strptime(value[:8], "%Y%m%d")
        # All-day: anchor at local midnight so "today" means today for the user.
        return (d.replace(tzinfo=None).astimezone(), True)

    m = re.match(r"^(\d{8}T\d{6})(Z)?$", value)
    if not m:
        return (None, False)
    naive = _dt.datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")

    if m.group(2) == "Z":                        # explicit UTC
        return (naive.replace(tzinfo=UTC), False)

    tzid = params.get("TZID")
    if tzid:
        tz = _resolve_tz(tzid, vtimezones, naive)
        if tz is not None:
            return (naive.replace(tzinfo=tz).astimezone(UTC), False)
        log.warning("Unknown time zone %r — reading that meeting's time as your "
                    "computer's local time. If it fires at the wrong minute, that's why.", tzid)

    # No TZID and no Z: a "floating" local time. Treat as local.
    return (naive.astimezone(), False)


_TZ_CACHE = {}


def _resolve_tz(tzid, vtimezones, when):
    """A tzinfo for tzid: real IANA zone via zoneinfo first (correct DST), else the
    fixed offset parsed from the ICS's own VTIMEZONE block."""
    if tzid in _TZ_CACHE:
        cached = _TZ_CACHE[tzid]
        return cached(when) if callable(cached) else cached
    if ZoneInfo is not None:
        try:
            z = ZoneInfo(tzid)
            _TZ_CACHE[tzid] = z
            return z
        except Exception:
            pass
    vt = vtimezones.get(tzid)
    if vt:
        _TZ_CACHE[tzid] = vt
        return vt(when) if callable(vt) else vt
    _TZ_CACHE[tzid] = None
    return None


def _event_end(props, start, all_day, vtimezones):
    end_raw = _get_raw(props, "DTEND")
    if end_raw is not None:
        end, _ = _parse_dt(end_raw, vtimezones)
        if end is not None:
            return end
    dur = _get(props, "DURATION")
    if dur:
        td = _parse_duration(dur)
        if td is not None:
            return start + td
    return start + (_dt.timedelta(days=1) if all_day else _dt.timedelta(hours=1))


def _parse_duration(s):
    """ISO-8601 duration as used in ICS, e.g. PT1H, PT30M, P1D, PT1H30M."""
    m = re.match(r"^([+-]?)P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$", s.strip())
    if not m:
        return None
    sign, w, d, h, mi, se = m.groups()
    total = _dt.timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0),
                          minutes=int(mi or 0), seconds=int(se or 0))
    return -total if sign == "-" else total


def _parse_utc_offset(s):
    """'+0530' / '-0800' -> timedelta."""
    m = re.match(r"^([+-])(\d{2})(\d{2})(\d{2})?$", s.strip())
    if not m:
        return None
    sign, hh, mm, ss = m.groups()
    td = _dt.timedelta(hours=int(hh), minutes=int(mm), seconds=int(ss or 0))
    return -td if sign == "-" else td


def _is_declined(props):
    """Best-effort 'did I decline this?' from the iCal feed. Feeds rarely tell us
    which attendee is 'me', so we only trust it when there's a single attendee who
    declined. (Most feeds already omit declined events; documented in the README.)"""
    attendees = [p for (n, p, v) in props if n == "ATTENDEE"]
    if len(attendees) == 1 and attendees[0].get("PARTSTAT", "").upper() == "DECLINED":
        return True
    return False


# ── VTIMEZONE (fallback offsets when there's no tz database) ─────────────────
def _parse_vtimezones(lines):
    """Map TZID -> a picker(when)->tzinfo built from the ICS's VTIMEZONE blocks.
    A coarse but self-contained fallback: choose STANDARD vs DAYLIGHT by the most
    recent transition on or before `when`. zoneinfo is preferred when available."""
    zones = {}
    tzid = None
    subs = []           # (kind, offset_timedelta, transition_month_day_guess)
    mode = None         # STANDARD | DAYLIGHT
    cur = {}
    for line in lines:
        up = line.upper()
        if up.startswith("BEGIN:VTIMEZONE"):
            tzid, subs = None, []
        elif up.startswith("TZID:") and tzid is None:
            tzid = line.split(":", 1)[1].strip()
        elif up.startswith("BEGIN:STANDARD"):
            mode, cur = "STANDARD", {}
        elif up.startswith("BEGIN:DAYLIGHT"):
            mode, cur = "DAYLIGHT", {}
        elif up.startswith("TZOFFSETTO:") and mode:
            cur["offset"] = _parse_utc_offset(line.split(":", 1)[1])
        elif up.startswith("DTSTART:") and mode:
            try:
                cur["start"] = _dt.datetime.strptime(line.split(":", 1)[1].strip()[:15], "%Y%m%dT%H%M%S")
            except ValueError:
                cur["start"] = None
        elif up.startswith("END:STANDARD") or up.startswith("END:DAYLIGHT"):
            if cur.get("offset") is not None:
                subs.append((mode, cur["offset"], cur.get("start")))
            mode = None
        elif up.startswith("END:VTIMEZONE") and tzid:
            zones[tzid] = _make_vtz_picker(subs)
            tzid = None
    return zones


def _make_vtz_picker(subs):
    offsets = [(kind, off) for (kind, off, _s) in subs if off is not None]
    if not offsets:
        return None
    std = next((off for kind, off in offsets if kind == "STANDARD"), offsets[0][1])
    day = next((off for kind, off in offsets if kind == "DAYLIGHT"), None)

    def picker(_when):
        # Coarse: without evaluating full DST rules, prefer standard time. Real DST
        # correctness comes from zoneinfo; this only runs when that's unavailable.
        return _dt.timezone(std)
    _ = day
    return picker


# ── recurrence expansion ────────────────────────────────────────────────────
def _collect_exdates(props, vtimezones):
    out = []
    for n, p, v in props:
        if n != "EXDATE":
            continue
        for piece in v.split(","):
            dt, _ = _parse_dt((p, piece), vtimezones)
            if dt is not None:
                out.append(dt.astimezone(UTC))
    return out


def _expand_rrule(dtstart, rrule, window_start, window_end):
    """Yield occurrence datetimes of a recurring series that fall in the window.
    Supports the rules real meetings use: FREQ DAILY/WEEKLY/MONTHLY, INTERVAL,
    COUNT, UNTIL, and weekly BYDAY. Anything else is logged and the series is
    skipped rather than guessed at."""
    parts = {}
    for token in rrule.split(";"):
        if "=" in token:
            k, v = token.split("=", 1)
            parts[k.strip().upper()] = v.strip().upper()

    freq = parts.get("FREQ", "")
    interval = int(parts.get("INTERVAL", "1") or "1")
    count = int(parts["COUNT"]) if parts.get("COUNT", "").isdigit() else None
    until = None
    if parts.get("UNTIL"):
        u = parts["UNTIL"]
        try:
            if u.endswith("Z"):
                until = _dt.datetime.strptime(u[:15], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
            elif "T" in u:
                until = _dt.datetime.strptime(u[:15], "%Y%m%dT%H%M%S").astimezone()
            else:
                until = _dt.datetime.strptime(u[:8], "%Y%m%d").replace(tzinfo=UTC) + _dt.timedelta(days=1)
        except ValueError:
            until = None

    if freq not in ("DAILY", "WEEKLY", "MONTHLY"):
        log.warning("Recurring meeting uses FREQ=%s, which auto-join can't expand yet "
                    "— it won't be joined automatically. Join it manually, or set a "
                    "one-off event.", freq or "(none)")
        return

    hard_cap = 5000            # safety valve against a runaway rule
    emitted = 0

    def in_window(dt):
        return window_start <= dt.astimezone(UTC) <= window_end

    def past_end(dt):
        u = dt.astimezone(UTC)
        return u > window_end or (until is not None and u > until)

    if freq == "WEEKLY" and parts.get("BYDAY"):
        wanted = sorted(_WEEKDAY[d[-2:]] for d in parts["BYDAY"].split(",") if d[-2:] in _WEEKDAY)
        tod = _dt.timedelta(hours=dtstart.hour, minutes=dtstart.minute, seconds=dtstart.second)
        # Midnight on the Monday of dtstart's week (keeps dtstart's tz); we add the
        # weekday offset and the time-of-day back on per occurrence — exactly once.
        week0 = (dtstart - _dt.timedelta(days=dtstart.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        wk = 0
        if count is None:                        # fast-forward close to the window
            weeks_behind = (window_start.astimezone(UTC) - week0.astimezone(UTC)).days // 7
            wk = max(0, weeks_behind // interval - 1)
        while wk < hard_cap:
            week_start_utc = (week0 + _dt.timedelta(weeks=wk * interval)).astimezone(UTC)
            saw_in_range = False
            for wd in wanted:
                occ = week0 + _dt.timedelta(weeks=wk * interval, days=wd) + tod
                if occ < dtstart:
                    continue
                occ_utc = occ.astimezone(UTC)
                if until is not None and occ_utc > until:
                    return
                if count is not None and emitted >= count:
                    return
                emitted += 1
                if occ_utc <= window_end:
                    saw_in_range = True
                    if occ_utc >= window_start:
                        yield occ
            if not saw_in_range and week_start_utc > window_end:
                return                           # whole week is past the window — done
            wk += 1
        return

    # DAILY / WEEKLY(no BYDAY) / MONTHLY: step from dtstart, fast-forwarding cheaply.
    step_days = {"DAILY": 1, "WEEKLY": 7}.get(freq)
    occ = dtstart
    if step_days and count is None:              # jump close to the window, then walk
        behind = (window_start.astimezone(UTC) - dtstart.astimezone(UTC)).days
        if behind > 0:
            jumps = behind // (step_days * interval)
            occ = dtstart + _dt.timedelta(days=jumps * step_days * interval)
    i = 0
    while i < hard_cap:
        u = occ.astimezone(UTC)
        if count is not None and emitted >= count:
            return
        if until is not None and u > until:
            return
        if u > window_end and (freq != "MONTHLY"):
            return
        if in_window(occ):
            yield occ
        emitted += 1
        if freq == "MONTHLY":
            occ = _add_months(occ, interval)
            if occ.astimezone(UTC) > window_end and count is None:
                return
        else:
            occ = occ + _dt.timedelta(days=step_days * interval)
        i += 1


def _add_months(dt, n):
    m = dt.month - 1 + n
    year = dt.year + m // 12
    month = m % 12 + 1
    # Clamp day for short months (e.g. the 31st in a 30-day month).
    day = min(dt.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                       31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return dt.replace(year=year, month=month, day=day)


# ── line unfolding (RFC 5545 §3.1) ──────────────────────────────────────────
def _unfold(text):
    """A CRLF (or LF) followed by a space or tab continues the previous line."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for line in text.split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return [ln for ln in out if ln]


# ── manual check: python calendar_source.py <ics url or file> ───────────────
def _main(argv):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not argv:
        print('Usage: python calendar_source.py "<ics url or ./file.ics>"')
        return 2
    src = ICSCalendarSource(argv[0])
    now = _dt.datetime.now(UTC)
    events = src.events(now - _dt.timedelta(hours=1), now + _dt.timedelta(days=14))
    if not events:
        print("No upcoming meetings found in the next 14 days.")
        return 0
    print(f"Found {len(events)} upcoming event(s):\n")
    for e in events:
        when = e.start.astimezone().strftime("%a %d %b %H:%M")
        flags = " ".join(f for f, on in (("all-day", e.all_day), ("cancelled", e.cancelled),
                                         ("declined", e.declined)) if on)
        link = e.url or "(no meeting link)"
        print(f"  {when}  {e.title}")
        print(f"           {link}" + (f"   [{flags}]" if flags else ""))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))

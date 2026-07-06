"use strict";
/*
 * Calendar sources for the auto-join scheduler (Node mirror of python/calendar_source.py).
 *
 * One job: answer "what meetings are coming up, and what's the link?" from a
 * calendar you connect once. Today that's a private iCal/ICS feed (Google, Outlook,
 * Apple — every provider exposes one). No dependencies: fetch the feed, unfold +
 * parse the VEVENTs, expand recurring meetings, and pull the Meet / Zoom / Teams
 * link out of each.
 *
 * The OAuth seam: everything the scheduler needs is CalendarSource.events(). A
 * GoogleCalendarSource (live Calendar API, OAuth) can implement the same method
 * later and drop straight in. Pick the source with makeSource(); it reads
 * CALENDAR.SOURCE from config.jsonc.
 *
 * Manual check:  node calendar-source.js "<ics url or ./file.ics>"
 */
const fs = require("fs");

let log = { warn: (...a) => console.error(...a), info: () => {} };
function setLogger(l) { log = l; }

// ── meeting-link detection (ordered by provider) ────────────────────────────
const LINK_PATTERNS = [
  /https:\/\/meet\.google\.com\/[a-z]{3}-[a-z]{4}-[a-z]{3}\b/i,
  /https:\/\/meet\.google\.com\/lookup\/[A-Za-z0-9]+/i,
  /https:\/\/[A-Za-z0-9.-]*zoom\.us\/(?:j|w|my|s)\/[^\s"'<>]+/i,
  /https:\/\/teams\.microsoft\.com\/l\/meetup-join\/[^\s"'<>]+/i,
  /https:\/\/teams\.live\.com\/meet\/[^\s"'<>]+/i,
  /https:\/\/[A-Za-z0-9.-]*webex\.com\/[^\s"'<>]+/i,
];
const WEEKDAY = { MO: 0, TU: 1, WE: 2, TH: 3, FR: 4, SA: 5, SU: 6 };

function icsUnescape(s) {
  return s.replace(/\\n/gi, "\n").replace(/\\,/g, ",").replace(/\\;/g, ";").replace(/\\\\/g, "\\");
}

function extractLink(...texts) {
  const blob = texts.filter(Boolean).map(icsUnescape).join("\n");
  for (const pat of LINK_PATTERNS) {
    const m = blob.match(pat);
    if (m) return m[0].replace(/[.,);\]>]+$/, "");   // trim trailing prose punctuation
  }
  return null;
}

// ── Event ───────────────────────────────────────────────────────────────────
class Event {
  constructor({ uid, title, start, end, url, allDay, cancelled, declined }) {
    this.uid = uid; this.title = title;
    this.start = start; this.end = end;       // Date (UTC instant) or null
    this.url = url; this.allDay = allDay;
    this.cancelled = cancelled; this.declined = declined;
  }
  key() {
    const stamp = this.start
      ? this.start.toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z")
      : "?";
    return `${this.uid}::${stamp}`;
  }
}

// ── the source interface (the OAuth seam) ───────────────────────────────────
class CalendarSource {
  async events(_windowStart, _windowEnd) { throw new Error("not implemented"); }
  describe() { return this.constructor.name; }
}

class ICSCalendarSource extends CalendarSource {
  constructor(url, timeoutMs = 20000) { super(); this.url = url; this.timeoutMs = timeoutMs; }
  describe() { return "iCal feed"; }

  async _fetch() {
    let u = this.url;
    if (!/^[a-z]+:\/\//i.test(u)) return fs.readFileSync(u, "utf-8");   // local path (tests/offline)
    if (u.startsWith("webcal://")) u = "https://" + u.slice("webcal://".length);
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), this.timeoutMs);
    try {
      const resp = await fetch(u, {
        signal: ctrl.signal,
        headers: { "User-Agent": "meeting-notetaker/1.0 (+agentcall.dev)" },
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status} fetching the calendar feed`);
      return await resp.text();
    } finally { clearTimeout(t); }
  }

  async events(windowStart, windowEnd) {
    return parseIcs(await this._fetch(), windowStart, windowEnd);
  }
}

function makeSource(calConfig, env) {
  const source = ((calConfig || {}).SOURCE || "ics").toLowerCase();
  if (source === "ics") {
    const url = (env.CALENDAR_ICS_URL || "").trim();
    if (!url) {
      throw new Error(
        "No calendar link set. Add CALENDAR_ICS_URL to .env (the builder does this " +
        "for you: npm run build), or run node autojoin.js connect.");
    }
    return new ICSCalendarSource(url);
  }
  throw new Error(`Unknown CALENDAR.SOURCE "${source}" in config.jsonc (supported: "ics").`);
}

// ── timezone helpers (Node ships full ICU) ──────────────────────────────────
const _tzValid = new Map();
function isValidTz(tzid) {
  if (_tzValid.has(tzid)) return _tzValid.get(tzid);
  let ok = true;
  try { new Intl.DateTimeFormat("en-US", { timeZone: tzid }); } catch { ok = false; }
  _tzValid.set(tzid, ok);
  return ok;
}
function tzOffsetMinutes(instantMs, tzid) {
  const dtf = new Intl.DateTimeFormat("en-US", {
    timeZone: tzid, hour12: false,
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
  const p = {};
  for (const { type, value } of dtf.formatToParts(new Date(instantMs))) p[type] = value;
  let hour = parseInt(p.hour, 10); if (hour === 24) hour = 0;
  const asUTC = Date.UTC(+p.year, +p.month - 1, +p.day, hour, +p.minute, +p.second);
  return (asUTC - instantMs) / 60000;          // minutes the zone is ahead of UTC
}
function wallToUtcViaTz(f, tzid) {
  const guess = Date.UTC(f.y, f.mo - 1, f.d, f.h, f.mi, f.s);
  let off = tzOffsetMinutes(guess, tzid);
  let t = guess - off * 60000;
  off = tzOffsetMinutes(t, tzid);              // refine across a DST edge
  return guess - off * 60000;
}

// Turn wall-clock fields + a "kind" (utc | <tzid> | local) into a real UTC instant.
function realize(f, kind, vtz) {
  if (kind === "utc") return new Date(Date.UTC(f.y, f.mo - 1, f.d, f.h, f.mi, f.s));
  if (kind && kind !== "local") {
    if (isValidTz(kind)) return new Date(wallToUtcViaTz(f, kind));
    const off = vtz[kind];
    if (off !== undefined && off !== null) {
      return new Date(Date.UTC(f.y, f.mo - 1, f.d, f.h, f.mi, f.s) - off * 60000);
    }
    log.warn(`Unknown time zone "${kind}" — reading that meeting's time as this ` +
             `computer's local time. If it fires at the wrong minute, that's why.`);
  }
  return new Date(f.y, f.mo - 1, f.d, f.h, f.mi, f.s);   // local
}

// ── ICS parsing ─────────────────────────────────────────────────────────────
function parseIcs(text, windowStart, windowEnd) {
  const lines = unfold(text);
  const vtz = parseVtimezones(lines);

  const rawEvents = [];
  const stack = [];
  let cur = null;
  for (const line of lines) {
    const up = line.toUpperCase();
    if (up.startsWith("BEGIN:")) {
      const name = line.split(":", 2)[1].trim().toUpperCase();
      stack.push(name);
      if (name === "VEVENT") cur = [];
      continue;
    }
    if (up.startsWith("END:")) {
      const name = stack.pop() || "";
      if (name === "VEVENT" && cur) { rawEvents.push(cur); cur = null; }
      continue;
    }
    if (cur && stack[stack.length - 1] === "VEVENT") cur.push(parseProp(line));
  }

  const overrides = new Set();
  for (const props of rawEvents) {
    const rid = getRaw(props, "RECURRENCE-ID");
    if (rid) {
      const dt = parseDt(rid, vtz).date;
      if (dt) overrides.add(`${get(props, "UID") || ""}@${Math.round(dt.getTime() / 60000)}`);
    }
  }

  const out = [];
  for (const props of rawEvents) {
    try {
      for (const ev of eventsFromVevent(props, vtz, overrides, windowStart, windowEnd)) out.push(ev);
    } catch (e) {
      const title = get(props, "SUMMARY") || "(untitled)";
      log.warn(`Skipped a calendar event "${title}" — couldn't parse it (${e.message}). ` +
               `Join that one manually if you need it.`);
    }
  }
  out.sort((a, b) => (a.start ? a.start.getTime() : windowEnd.getTime()) -
                     (b.start ? b.start.getTime() : windowEnd.getTime()));
  return out;
}

function eventsFromVevent(props, vtz, overrides, windowStart, windowEnd) {
  const uid = get(props, "UID") || "";
  const title = icsUnescape(get(props, "SUMMARY") || "(untitled)");
  const cancelled = (get(props, "STATUS") || "").toUpperCase() === "CANCELLED";
  const url = extractLink(get(props, "X-GOOGLE-CONFERENCE"), get(props, "LOCATION"),
                          get(props, "DESCRIPTION"), get(props, "URL"));
  const declined = isDeclined(props);

  const startRaw = getRaw(props, "DTSTART");
  if (!startRaw) return [];
  const parsed = parseDt(startRaw, vtz);
  if (!parsed.date) { log.warn(`Event "${title}" has an unreadable start time — skipping it.`); return []; }

  const end = eventEnd(props, parsed, vtz);
  const spanMs = end ? (end.getTime() - parsed.date.getTime()) : 3600000;

  const mk = (startDate) => new Event({
    uid, title, start: startDate, end: new Date(startDate.getTime() + spanMs),
    url, allDay: parsed.allDay, cancelled, declined,
  });

  const rid = get(props, "RECURRENCE-ID");
  const rrule = get(props, "RRULE");
  if (!rrule || rid) {
    const t = parsed.date.getTime();
    return (t >= windowStart.getTime() && t <= windowEnd.getTime()) ? [mk(parsed.date)] : [];
  }

  const exdates = collectExdates(props, vtz).map((d) => Math.round(d.getTime() / 60000));
  const events = [];
  for (const occ of expandRrule(parsed, rrule, vtz, windowStart, windowEnd)) {
    const minute = Math.round(occ.getTime() / 60000);
    if (exdates.includes(minute)) continue;
    if (overrides.has(`${uid}@${minute}`)) continue;
    events.push(mk(occ));
  }
  return events;
}

// ── property + datetime helpers ─────────────────────────────────────────────
function parseProp(line) {
  const idx = line.indexOf(":");
  const head = idx < 0 ? line : line.slice(0, idx);
  const value = idx < 0 ? "" : line.slice(idx + 1);
  const parts = head.split(";");
  const name = parts[0].trim().toUpperCase();
  const params = {};
  for (const p of parts.slice(1)) {
    const eq = p.indexOf("=");
    if (eq > 0) params[p.slice(0, eq).trim().toUpperCase()] = p.slice(eq + 1).trim().replace(/^"|"$/g, "");
  }
  return { name, params, value };
}
function get(props, name) {
  name = name.toUpperCase();
  const hit = props.find((p) => p.name === name);
  return hit ? hit.value : null;
}
function getRaw(props, name) {
  name = name.toUpperCase();
  return props.find((p) => p.name === name) || null;
}

// Returns { date: Date|null, allDay: bool, fields, kind } — fields/kind drive recurrence.
function parseDt(raw, vtz) {
  if (!raw) return { date: null, allDay: false };
  const value = (raw.value || "").trim();
  const params = raw.params || {};

  if ((params.VALUE || "").toUpperCase() === "DATE" || /^\d{8}$/.test(value)) {
    const f = { y: +value.slice(0, 4), mo: +value.slice(4, 6), d: +value.slice(6, 8), h: 0, mi: 0, s: 0 };
    return { date: realize(f, "local", vtz), allDay: true, fields: f, kind: "local" };
  }
  const m = value.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z)?$/);
  if (!m) return { date: null, allDay: false };
  const f = { y: +m[1], mo: +m[2], d: +m[3], h: +m[4], mi: +m[5], s: +m[6] };
  const kind = m[7] ? "utc" : (params.TZID || "local");
  return { date: realize(f, kind, vtz), allDay: false, fields: f, kind };
}

function eventEnd(props, startParsed, vtz) {
  const endRaw = getRaw(props, "DTEND");
  if (endRaw) { const e = parseDt(endRaw, vtz).date; if (e) return e; }
  const dur = get(props, "DURATION");
  if (dur) { const ms = parseDuration(dur); if (ms !== null) return new Date(startParsed.date.getTime() + ms); }
  return new Date(startParsed.date.getTime() + (startParsed.allDay ? 86400000 : 3600000));
}
function parseDuration(s) {
  const m = s.trim().match(/^([+-]?)P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$/);
  if (!m) return null;
  const [, sign, w, d, h, mi, se] = m;
  let ms = ((+w || 0) * 7 * 86400 + (+d || 0) * 86400 + (+h || 0) * 3600 + (+mi || 0) * 60 + (+se || 0)) * 1000;
  return sign === "-" ? -ms : ms;
}
function parseUtcOffset(s) {
  const m = s.trim().match(/^([+-])(\d{2})(\d{2})(\d{2})?$/);
  if (!m) return null;
  const mins = (+m[2]) * 60 + (+m[3]);
  return m[1] === "-" ? -mins : mins;
}
function isDeclined(props) {
  const att = props.filter((p) => p.name === "ATTENDEE");
  return att.length === 1 && (att[0].params.PARTSTAT || "").toUpperCase() === "DECLINED";
}

// ── VTIMEZONE fallback offsets (used only when Intl can't resolve a TZID) ────
function parseVtimezones(lines) {
  const zones = {};
  let tzid = null, mode = null, cur = {};
  for (const line of lines) {
    const up = line.toUpperCase();
    if (up.startsWith("BEGIN:VTIMEZONE")) { tzid = null; }
    else if (up.startsWith("TZID:") && tzid === null) tzid = line.split(":", 2)[1].trim();
    else if (up.startsWith("BEGIN:STANDARD")) { mode = "STANDARD"; cur = {}; }
    else if (up.startsWith("BEGIN:DAYLIGHT")) { mode = "DAYLIGHT"; cur = {}; }
    else if (up.startsWith("TZOFFSETTO:") && mode) cur.offset = parseUtcOffset(line.split(":", 2)[1]);
    else if ((up.startsWith("END:STANDARD") || up.startsWith("END:DAYLIGHT")) && mode) {
      if (cur.offset !== undefined && cur.offset !== null && (zones[tzid] === undefined || mode === "STANDARD")) {
        zones[tzid] = cur.offset;               // prefer STANDARD offset; coarse but self-contained
      }
      mode = null;
    }
  }
  return zones;
}

// ── recurrence expansion (wall-clock domain, then realized per occurrence) ──
function collectExdates(props, vtz) {
  const out = [];
  for (const p of props) {
    if (p.name !== "EXDATE") continue;
    for (const piece of p.value.split(",")) {
      const dt = parseDt({ params: p.params, value: piece }, vtz).date;
      if (dt) out.push(dt);
    }
  }
  return out;
}

function* expandRrule(startParsed, rrule, vtz, windowStart, windowEnd) {
  const parts = {};
  for (const tok of rrule.split(";")) { const i = tok.indexOf("="); if (i > 0) parts[tok.slice(0, i).trim().toUpperCase()] = tok.slice(i + 1).trim().toUpperCase(); }
  const freq = parts.FREQ || "";
  const interval = parseInt(parts.INTERVAL || "1", 10) || 1;
  const count = /^\d+$/.test(parts.COUNT || "") ? parseInt(parts.COUNT, 10) : null;
  let until = null;
  if (parts.UNTIL) {
    const u = parts.UNTIL;
    if (/^\d{8}T\d{6}Z$/.test(u)) until = new Date(Date.UTC(+u.slice(0, 4), +u.slice(4, 6) - 1, +u.slice(6, 8), +u.slice(9, 11), +u.slice(11, 13), +u.slice(13, 15)));
    else if (/^\d{8}/.test(u)) until = new Date(Date.UTC(+u.slice(0, 4), +u.slice(4, 6) - 1, +u.slice(6, 8)) + 86400000);
  }

  if (!["DAILY", "WEEKLY", "MONTHLY"].includes(freq)) {
    log.warn(`Recurring meeting uses FREQ=${freq || "(none)"}, which auto-join can't expand yet — ` +
             `it won't be joined automatically. Join it manually, or make a one-off event.`);
    return;
  }

  const kind = startParsed.kind, f0 = startParsed.fields;
  const DAY = 86400000, HARD_CAP = 6000;
  const tod = ((f0.h * 3600) + (f0.mi * 60) + f0.s) * 1000;
  const startWall = Date.UTC(f0.y, f0.mo - 1, f0.d, f0.h, f0.mi, f0.s);
  const wsUtc = windowStart.getTime(), weUtc = windowEnd.getTime();
  let emitted = 0;

  const fieldsOf = (wallMs) => { const w = new Date(wallMs); return { y: w.getUTCFullYear(), mo: w.getUTCMonth() + 1, d: w.getUTCDate(), h: w.getUTCHours(), mi: w.getUTCMinutes(), s: w.getUTCSeconds() }; };

  if (freq === "WEEKLY" && parts.BYDAY) {
    const wanted = parts.BYDAY.split(",").map((d) => WEEKDAY[d.slice(-2)]).filter((n) => n !== undefined).sort((a, b) => a - b);
    const s0 = new Date(startWall);
    const dow = (s0.getUTCDay() + 6) % 7;       // Mon=0
    const week0 = Date.UTC(s0.getUTCFullYear(), s0.getUTCMonth(), s0.getUTCDate()) - dow * DAY;
    let wk = 0;
    if (count === null) { const behind = Math.floor((wsUtc - week0) / (7 * DAY)); wk = Math.max(0, Math.floor(behind / interval) - 1); }
    while (wk < HARD_CAP) {
      const weekStartUtc = realize(fieldsOf(week0 + wk * interval * 7 * DAY), kind, vtz).getTime();
      let sawInRange = false;
      for (const wd of wanted) {
        const occWall = week0 + wk * interval * 7 * DAY + wd * DAY + tod;
        if (occWall < startWall) continue;
        const occ = realize(fieldsOf(occWall), kind, vtz);
        const oUtc = occ.getTime();
        if (until && oUtc > until.getTime()) return;
        if (count !== null && emitted >= count) return;
        emitted += 1;
        if (oUtc <= weUtc) { sawInRange = true; if (oUtc >= wsUtc) yield occ; }
      }
      if (!sawInRange && weekStartUtc > weUtc) return;
      wk += 1;
    }
    return;
  }

  const stepDays = freq === "DAILY" ? 1 : (freq === "WEEKLY" ? 7 : null);
  let occWall = startWall;
  if (stepDays && count === null) {
    const behind = Math.floor((wsUtc - realize(f0, kind, vtz).getTime()) / DAY);
    if (behind > 0) occWall = startWall + Math.floor(behind / (stepDays * interval)) * stepDays * interval * DAY;
  }
  let i = 0;
  while (i < HARD_CAP) {
    const occ = realize(fieldsOf(occWall), kind, vtz);
    const oUtc = occ.getTime();
    if (count !== null && emitted >= count) return;
    if (until && oUtc > until.getTime()) return;
    if (oUtc > weUtc && freq !== "MONTHLY") return;
    if (oUtc >= wsUtc && oUtc <= weUtc) yield occ;
    emitted += 1;
    if (freq === "MONTHLY") {
      occWall = addMonths(occWall, interval);
      if (realize(fieldsOf(occWall), kind, vtz).getTime() > weUtc && count === null) return;
    } else {
      occWall += stepDays * interval * DAY;
    }
    i += 1;
  }
}

function addMonths(wallMs, n) {
  const w = new Date(wallMs);
  let m = w.getUTCMonth() + n;
  const year = w.getUTCFullYear() + Math.floor(m / 12);
  const month = ((m % 12) + 12) % 12;
  const dim = [31, (year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month];
  const day = Math.min(w.getUTCDate(), dim);
  return Date.UTC(year, month, day, w.getUTCHours(), w.getUTCMinutes(), w.getUTCSeconds());
}

// ── line unfolding (RFC 5545 §3.1) ──────────────────────────────────────────
function unfold(text) {
  text = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  const out = [];
  for (const line of text.split("\n")) {
    if ((line[0] === " " || line[0] === "\t") && out.length) out[out.length - 1] += line.slice(1);
    else if (line) out.push(line);
  }
  return out;
}

// ── manual check ────────────────────────────────────────────────────────────
async function main(argv) {
  if (!argv.length) { console.log('Usage: node calendar-source.js "<ics url or ./file.ics>"'); return 2; }
  const src = new ICSCalendarSource(argv[0]);
  const now = new Date();
  const events = await src.events(new Date(now.getTime() - 3600000), new Date(now.getTime() + 14 * 86400000));
  if (!events.length) { console.log("No upcoming meetings found in the next 14 days."); return 0; }
  console.log(`Found ${events.length} upcoming event(s):\n`);
  for (const e of events) {
    const when = e.start.toLocaleString(undefined, { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
    const flags = [["all-day", e.allDay], ["cancelled", e.cancelled], ["declined", e.declined]].filter(([, on]) => on).map(([f]) => f).join(" ");
    console.log(`  ${when}  ${e.title}`);
    console.log(`           ${e.url || "(no meeting link)"}${flags ? `   [${flags}]` : ""}`);
  }
  return 0;
}

module.exports = { Event, CalendarSource, ICSCalendarSource, makeSource, parseIcs, extractLink, setLogger };

if (require.main === module) {
  main(process.argv.slice(2)).then((c) => process.exit(c)).catch((e) => { console.error(e.message); process.exit(1); });
}

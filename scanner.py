#!/usr/bin/env python3
"""Bandcamp Release Scanner.

Watches Gmail (over IMAP) for Bandcamp "New release from ..." emails, scrapes each
linked release page for its player ID, release date and physical formats, and serves
a local screening UI at http://localhost:8765.

Standard library only. Settings come from config.json next to this file, overridden by
environment variables (see ENV_SETTINGS; this is how the Docker deployment is configured).
The Gmail app password comes from GMAIL_APP_PASSWORD or, on a Mac, the Keychain item
"bandcamp-scanner".
"""

import email
import email.policy
import email.utils
import html
import imaplib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("BCS_DB", ROOT / "releases.db"))
CONFIG_PATH = ROOT / "config.json"
STATIC_DIR = ROOT / "static"
KEYCHAIN_SERVICE = "bandcamp-scanner"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

DEFAULT_CONFIG = {
    "gmail_address": "",
    "host": "127.0.0.1",
    "port": 8765,
    "poll_seconds": 60,
    "backfill_days": 2,
    "notify": True,
    "mark_read": True,
    "gmail_label": "bandcamp_parsed",
    "public_url": "",
    "gotify_url": "",
    "gotify_token": "",
    "gotify_priority": 5,
}

# Environment variable -> (config key, type)
ENV_SETTINGS = {
    "GMAIL_ADDRESS": ("gmail_address", str),
    "BCS_HOST": ("host", str),
    "BCS_PORT": ("port", int),
    "POLL_SECONDS": ("poll_seconds", int),
    "BACKFILL_DAYS": ("backfill_days", int),
    "NOTIFY": ("notify", bool),
    "MARK_READ": ("mark_read", bool),
    "GMAIL_LABEL": ("gmail_label", str),
    "PUBLIC_URL": ("public_url", str),
    "GOTIFY_URL": ("gotify_url", str),
    "GOTIFY_TOKEN": ("gotify_token", str),
    "GOTIFY_PRIORITY": ("gotify_priority", int),
}

log = logging.getLogger("scanner")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    for var, (key, kind) in ENV_SETTINGS.items():
        value = os.environ.get(var)
        if value is None:
            continue
        cfg[key] = value.strip().lower() in ("1", "true", "yes", "on") if kind is bool else kind(value)
    return cfg


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- db

SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
    id              INTEGER PRIMARY KEY,
    url             TEXT NOT NULL,
    item_type       TEXT,
    item_id         INTEGER,
    title           TEXT,
    artist          TEXT,
    label           TEXT,
    sender          TEXT,
    email_subject   TEXT,
    email_verb      TEXT,
    email_blurb     TEXT,
    email_reason    TEXT,
    email_received  TEXT,
    gmail_link      TEXT,
    art_url         TEXT,
    release_date    TEXT,
    is_preorder     INTEGER DEFAULT 0,
    packages        TEXT DEFAULT '[]',
    tags            TEXT DEFAULT '[]',
    about           TEXT,
    track_count     INTEGER,
    duration        REAL,
    has_vinyl       INTEGER DEFAULT 0,
    has_merch       INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'new',
    status_changed  TEXT,
    scraped_at      TEXT,
    scrape_error    TEXT,
    scrape_attempts INTEGER DEFAULT 0,
    created_at      TEXT
);
CREATE TABLE IF NOT EXISTS emails (
    uidvalidity INTEGER,
    uid         INTEGER,
    release_id  INTEGER,
    marked      INTEGER DEFAULT 0,
    PRIMARY KEY (uidvalidity, uid)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS releases_url ON releases(url);
"""

# Emails for the same release closer together than this are merged into one card (e.g. the label and
# the artist both announcing it); a later email, such as release day after a pre-order, gets a new card.
MERGE_WINDOW_HOURS = 48

_db_lock = threading.RLock()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock, db() as conn:
        # Older databases had a UNIQUE url column; rebuild the table without it.
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='releases'").fetchone()
        if ddl and "UNIQUE" in ddl["sql"]:
            conn.execute("ALTER TABLE releases RENAME TO releases_old")
            conn.executescript(SCHEMA)
            conn.execute("INSERT INTO releases SELECT * FROM releases_old")
            conn.execute("DROP TABLE releases_old")
        conn.executescript(SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(emails)")}
        if "marked" not in cols:
            conn.execute("ALTER TABLE emails ADD COLUMN marked INTEGER DEFAULT 0")


def get_meta(key, default=None):
    with _db_lock, db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key, value):
    with _db_lock, db() as conn:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))


def bump_version():
    set_meta("version", int(get_meta("version", "0")) + 1)


# ----------------------------------------------------------------- email parse

RELEASE_LINK_RE = re.compile(r"""href=["'](https?://[^"'\s]+/(?:album|track)/[^"'\s]+)["']""", re.I)
PLAIN_LINK_RE = re.compile(r"(https?://\S+/(?:album|track)/\S+)")
BODY_RE = re.compile(
    r"^\s*(?P<sender>.+?) just (?P<verb>released|announced) \"(?P<title>.+)\"(?: by (?P<artist>.+?))?, check it out",
    re.M,
)


def canonical_url(url):
    parts = urlsplit(html.unescape(url).strip())
    return urlunsplit(("https", parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def _body_parts(msg):
    text = html_body = ""
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.is_multipart():
            continue
        try:
            content = part.get_content()
        except Exception:
            continue
        if ctype == "text/plain" and not text:
            text = content
        elif ctype == "text/html" and not html_body:
            html_body = content
    return text, html_body


def parse_bandcamp_email(raw_bytes):
    """Return a dict describing the release announced in a Bandcamp email, or None."""
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    subject = str(msg.get("Subject", "")).strip()
    if not subject.lower().startswith("new release from"):
        return None
    text, html_body = _body_parts(msg)

    url = None
    if html_body:
        m = RELEASE_LINK_RE.search(html_body)
        if m:
            url = m.group(1)
    if not url and text:
        m = PLAIN_LINK_RE.search(text)
        if m:
            url = m.group(1)
    if not url:
        return None

    plain = text or re.sub(r"<[^>]+>", " ", html_body)
    info = {"sender": None, "verb": "released", "title": None, "artist": None}
    m = BODY_RE.search(plain)
    if m:
        info.update({k: v for k, v in m.groupdict().items() if v})
    if not info["sender"]:
        info["sender"] = re.sub(r"^new release from\s+", "", subject, flags=re.I).split(", who brought you")[0]

    blurb = None
    m = re.search(r"“(.+?)”", plain, re.S)
    if m:
        blurb = re.sub(r"\s+", " ", m.group(1)).strip()
    reason = None
    m = re.search(r"(You received this because[^\n]+)", plain)
    if m:
        reason = m.group(1).strip()
    art = None
    if html_body:
        m = re.search(r"""src=["'](https://f\d\.bcbits\.com/img/[^"']+)["']""", html_body)
        if m:
            art = m.group(1)

    received = None
    if msg.get("Date"):
        try:
            received = email.utils.parsedate_to_datetime(msg["Date"]).astimezone(timezone.utc).isoformat(timespec="seconds")
        except Exception:
            pass

    return {
        "url": canonical_url(url),
        "subject": subject,
        "sender": info["sender"],
        "verb": info["verb"],
        "title": html.unescape(info["title"]) if info["title"] else None,
        "artist": info["artist"],
        "blurb": blurb,
        "reason": reason,
        "art_url": art,
        "received": received or now_iso(),
    }


# ---------------------------------------------------------------- page scrape

PHYSICAL_KINDS = [
    ("vinyl", ("vinyl", "lp", '12"', '7"', '10"', "record")),
    ("cd", ("compact disc", "cd")),
    ("cassette", ("cassette", "tape")),
]


def classify_package(pkg):
    type_name = (pkg.get("type_name") or "").lower()
    title = (pkg.get("title") or "").lower()
    if pkg.get("type_id") == 2 or "vinyl" in type_name or "vinyl" in title:
        return "vinyl"
    for kind, words in PHYSICAL_KINDS[1:]:
        if any(re.search(r"\b" + re.escape(w) + r"\b", type_name) for w in words):
            return kind
    # Box sets and bundles often contain records; fall back to the title.
    if re.search(r'\b(lp|2xlp|12"|7"|10"|vinyl)\b', title):
        return "vinyl"
    return "merch"


def parse_bc_date(value):
    if not value:
        return None
    for fmt in ("%d %b %Y %H:%M:%S %Z", "%d %b %Y %H:%M:%S GMT"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).date().isoformat()
        except ValueError:
            continue
    return None


def _data_attr(page, name):
    m = re.search(r'data-%s="([^"]+)"' % re.escape(name), page)
    return json.loads(html.unescape(m.group(1))) if m else None


def fetch_page(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def scrape_release(url):
    page = fetch_page(url)
    tralbum = _data_attr(page, "tralbum")
    if not tralbum:
        raise ValueError("no release data on page (is this an album/track URL?)")
    band = _data_attr(page, "band") or {}
    current = tralbum.get("current") or {}

    packages = []
    for p in tralbum.get("packages") or []:
        kind = classify_package(p)
        qty = p.get("quantity_available")
        packages.append({
            "kind": kind,
            "type_name": p.get("type_name"),
            "title": p.get("title"),
            "price": p.get("price"),
            "currency": p.get("currency"),
            "sold_out": qty == 0,
            "ships": parse_bc_date(p.get("release_date")),
            "image": ("https://f4.bcbits.com/img/%010d_10.jpg" % p["arts"][0]["image_id"]) if p.get("arts") else None,
        })

    tracks = tralbum.get("trackinfo") or []
    duration = sum((t.get("duration") or 0) for t in tracks)
    tags = [html.unescape(t).strip() for t in re.findall(r'<a class="tag"[^>]*>([^<]+)</a>', page)]
    release_date = parse_bc_date(tralbum.get("album_release_date") or current.get("release_date"))
    art_id = tralbum.get("art_id")

    return {
        "item_type": tralbum.get("item_type"),
        "item_id": tralbum.get("id"),
        "title": current.get("title"),
        "artist": tralbum.get("artist"),
        "label": band.get("name"),
        "art_url": "https://f4.bcbits.com/img/a%010d_10.jpg" % art_id if art_id else None,
        "release_date": release_date,
        "is_preorder": 1 if (tralbum.get("album_is_preorder") or tralbum.get("is_preorder")) else 0,
        "packages": packages,
        "tags": tags,
        "about": current.get("about"),
        "track_count": len(tracks),
        "duration": duration,
        "has_vinyl": int(any(p["kind"] == "vinyl" for p in packages)),
        "has_merch": int(any(p["kind"] == "merch" for p in packages)),
    }


def store_scrape(release_id, data):
    with _db_lock, db() as conn:
        conn.execute(
            """UPDATE releases SET item_type=?, item_id=?, title=COALESCE(?, title), artist=COALESCE(?, artist),
                   label=?, art_url=COALESCE(?, art_url), release_date=?, is_preorder=?, packages=?, tags=?,
                   about=?, track_count=?, duration=?, has_vinyl=?, has_merch=?, scraped_at=?, scrape_error=NULL
               WHERE id=?""",
            (data["item_type"], data["item_id"], data["title"], data["artist"], data["label"], data["art_url"],
             data["release_date"], data["is_preorder"], json.dumps(data["packages"]), json.dumps(data["tags"]),
             data["about"], data["track_count"], data["duration"], data["has_vinyl"], data["has_merch"],
             now_iso(), release_id),
        )


def scrape_pending(max_items=50):
    """Scrape releases that have never been scraped, failed (with backoff), or are
    upcoming and haven't been refreshed in 12h (release dates and formats change)."""
    with _db_lock, db() as conn:
        rows = conn.execute(
            """SELECT id, url, scrape_attempts FROM releases
               WHERE (scraped_at IS NULL AND scrape_attempts < 6)
                  OR (scraped_at IS NOT NULL AND status IN ('new','saved')
                      AND release_date >= date('now') AND scraped_at < datetime('now', '-12 hours'))
               ORDER BY email_received DESC LIMIT ?""",
            (max_items,),
        ).fetchall()
    changed = False
    for row in rows:
        try:
            store_scrape(row["id"], scrape_release(row["url"]))
            log.info("scraped %s", row["url"])
        except Exception as exc:  # network errors, 404s, layout changes
            log.warning("scrape failed %s: %s", row["url"], exc)
            with _db_lock, db() as conn:
                conn.execute("UPDATE releases SET scrape_error=?, scrape_attempts=scrape_attempts+1 WHERE id=?",
                             (str(exc)[:300], row["id"]))
        changed = True
        time.sleep(1.0)  # be polite to bandcamp
    if changed:
        bump_version()
    return len(rows)


def upsert_release(info, gmail_link=None):
    """Insert a release announced by an email; returns (release_id, is_new).

    An email for a release already on file within MERGE_WINDOW_HOURS is merged into that card;
    otherwise (e.g. the release-day email after a pre-order announcement) a new card is created."""
    received = info.get("received") or now_iso()
    with _db_lock, db() as conn:
        row = conn.execute(
            """SELECT id, sender FROM releases WHERE url=?
               AND abs(julianday(email_received) - julianday(?)) * 24 < ?
               ORDER BY email_received DESC LIMIT 1""",
            (info["url"], received, MERGE_WINDOW_HOURS),
        ).fetchone()
        if row:
            # Same release announced again (e.g. by both label and artist): note the extra sender.
            senders = [s.strip() for s in (row["sender"] or "").split(" + ") if s.strip()]
            if info.get("sender") and info["sender"] not in senders:
                conn.execute("UPDATE releases SET sender=? WHERE id=?",
                             (" + ".join(senders + [info["sender"]]), row["id"]))
            return row["id"], False
        cur = conn.execute(
            """INSERT INTO releases (url, title, artist, sender, email_subject, email_verb, email_blurb, email_reason,
                   email_received, gmail_link, art_url, label, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (info["url"], info.get("title"), info.get("artist"), info.get("sender"), info.get("subject"),
             info.get("verb"), info.get("blurb"), info.get("reason"), received,
             gmail_link, info.get("art_url"), info.get("sender"), now_iso()),
        )
        return cur.lastrowid, True


# ----------------------------------------------------------------- gmail poll

def get_app_password(address):
    if os.environ.get("GMAIL_APP_PASSWORD"):
        return os.environ["GMAIL_APP_PASSWORD"]
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", address, "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return None


def notify(cfg, title, message):
    """Send a notification via Gotify if configured, otherwise as a macOS notification."""
    try:
        if cfg.get("gotify_url") and cfg.get("gotify_token"):
            payload = {"title": title, "message": message, "priority": cfg.get("gotify_priority", 5)}
            if cfg.get("public_url"):
                payload["extras"] = {"client::notification": {"click": {"url": cfg["public_url"]}}}
            req = urllib.request.Request(
                cfg["gotify_url"].rstrip("/") + "/message", data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "X-Gotify-Key": cfg["gotify_token"]})
            urllib.request.urlopen(req, timeout=10).close()
        elif sys.platform == "darwin":
            script = 'display notification %s with title %s sound name "Glass"' % (json.dumps(message), json.dumps(title))
            subprocess.run(["osascript", "-e", script], capture_output=True)
    except Exception as exc:
        log.warning("notification failed: %s", exc)


class GmailWatcher:
    def __init__(self, cfg):
        self.cfg = cfg
        self.wake = threading.Event()
        self.status = {"state": "starting", "last_check": None, "error": None}

    def check_once(self):
        address = self.cfg["gmail_address"]
        password = get_app_password(address) if address else None
        if not address or not password:
            self.status.update(state="needs_setup", error="Gmail address or app password not configured (GMAIL_ADDRESS / GMAIL_APP_PASSWORD)")
            return []

        first_run = get_meta("backfilled") is None
        started = int(time.time())
        last_ok = get_meta("last_success")
        if first_run or not last_ok:
            window = "newer_than:%dd" % self.cfg["backfill_days"]
        else:
            # Search from the last successful check (minus a day of overlap for clock skew and late
            # delivery), so nothing is missed however long the Mac was asleep or off.
            window = "after:%d" % (int(last_ok) - 86400)
        query = 'from:noreply@bandcamp.com subject:(new release from) %s' % window

        new_ids = []
        imap = imaplib.IMAP4_SSL("imap.gmail.com")
        try:
            imap.login(address, password)
            # Search "All Mail" (named differently in some locales) so archived/filtered emails count too.
            mailbox = "INBOX"
            typ, boxes = imap.list()
            for line in boxes or []:
                if line and b"\\All" in line:
                    mailbox = '"%s"' % line.decode().rsplit(' "/" ', 1)[-1].strip('"')
                    break
            mark = self.cfg.get("mark_read") or self.cfg.get("gmail_label")
            imap.select(mailbox, readonly=not mark)
            uidvalidity = int((imap.response("UIDVALIDITY")[1] or [b"0"])[0])
            typ, data = imap.uid("SEARCH", "X-GM-RAW", '"%s"' % query)
            uids = [int(u) for u in (data[0] or b"").split()] if typ == "OK" else []
            with _db_lock, db() as conn:
                seen = {r["uid"] for r in conn.execute("SELECT uid FROM emails WHERE uidvalidity=?", (uidvalidity,))}
            todo = [u for u in uids if u not in seen]
            for uid in sorted(todo):
                typ, msg_data = imap.uid("FETCH", str(uid), "(X-GM-MSGID BODY.PEEK[])")
                raw = next((part[1] for part in msg_data if isinstance(part, tuple)), None)
                header = next((part[0] for part in msg_data if isinstance(part, tuple)), b"")
                release_id = None
                if raw:
                    info = parse_bandcamp_email(raw)
                    if info:
                        m = re.search(rb"X-GM-MSGID (\d+)", header)
                        link = "https://mail.google.com/mail/u/0/#all/%x" % int(m.group(1)) if m else None
                        release_id, is_new = upsert_release(info, link)
                        if is_new:
                            new_ids.append((release_id, info))
                with _db_lock, db() as conn:
                    conn.execute("INSERT OR IGNORE INTO emails(uidvalidity, uid, release_id) VALUES (?,?,?)",
                                 (uidvalidity, uid, release_id))
            if mark:
                self.mark_processed(imap, uidvalidity)
        finally:
            try:
                imap.logout()
            except Exception:
                pass

        if first_run:
            set_meta("backfilled", now_iso())
        set_meta("last_success", started)
        self.status.update(state="ok", last_check=now_iso(), error=None)
        if new_ids:
            bump_version()
            log.info("%d new release email(s)", len(new_ids))
            if self.cfg.get("notify") and not first_run:
                if len(new_ids) == 1:
                    info = new_ids[0][1]
                    notify(self.cfg, "New Bandcamp release", "%s — %s" % (info.get("sender"), info.get("title") or ""))
                else:
                    notify(self.cfg, "New Bandcamp releases", "%d new releases to screen" % len(new_ids))
        return new_ids

    def mark_processed(self, imap, uidvalidity):
        """Mark emails that were added to the scanner as read and apply the Gmail label.
        Emails that couldn't be parsed are left untouched so they still stand out in Gmail."""
        with _db_lock, db() as conn:
            uids = [r["uid"] for r in conn.execute(
                "SELECT uid FROM emails WHERE uidvalidity=? AND release_id IS NOT NULL AND marked=0", (uidvalidity,))]
        if not uids:
            return
        label = self.cfg.get("gmail_label")
        if label:
            imap.create(label)  # fails harmlessly if the label already exists
        for i in range(0, len(uids), 100):
            chunk = uids[i:i + 100]
            uid_set = ",".join(map(str, chunk))
            ok = True
            if self.cfg.get("mark_read"):
                ok &= imap.uid("STORE", uid_set, "+FLAGS", "(\\Seen)")[0] == "OK"
            if label:
                ok &= imap.uid("STORE", uid_set, "+X-GM-LABELS", '("%s")' % label)[0] == "OK"
            if ok:
                with _db_lock, db() as conn:
                    conn.executemany("UPDATE emails SET marked=1 WHERE uidvalidity=? AND uid=?",
                                     [(uidvalidity, u) for u in chunk])
        log.info("marked %d email(s) read / labelled %s", len(uids), label)

    def run(self):
        while True:
            try:
                self.check_once()
            except Exception as exc:
                log.exception("gmail check failed")
                self.status.update(state="error", error=str(exc)[:300], last_check=now_iso())
            try:
                scrape_pending()
            except Exception:
                log.exception("scrape pass failed")
            set_meta("status", json.dumps(self.status))
            self.wake.wait(self.cfg["poll_seconds"])
            self.wake.clear()


# ------------------------------------------------------------------------ http

def release_rows(status=None):
    with _db_lock, db() as conn:
        rows = conn.execute("SELECT * FROM releases ORDER BY email_received DESC").fetchall()
    out = []
    by_url = {}
    for r in rows:
        d = dict(r)
        d["packages"] = json.loads(d["packages"] or "[]")
        d["tags"] = json.loads(d["tags"] or "[]")
        by_url.setdefault(d["url"], []).append(d)
        out.append(d)
    # Tell each card about earlier emails for the same release (e.g. the pre-order announcement).
    for d in out:
        d["earlier"] = [{"id": e["id"], "received": e["email_received"], "verb": e["email_verb"], "status": e["status"]}
                        for e in by_url[d["url"]] if e["email_received"] < d["email_received"]]
    return out


class Handler(BaseHTTPRequestHandler):
    watcher = None
    cfg = None

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}") if length else {}

    def _local_only(self):
        # Reject cross-site requests so other web pages can't drive this API.
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and urlsplit(origin).netloc != host:
            self._json({"error": "forbidden"}, 403)
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            body = (STATIC_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/healthz":
            self._json({"ok": True, "gmail": self.watcher.status.get("state")})
        elif path == "/api/status":
            self._json({"version": int(get_meta("version", "0")), **self.watcher.status})
        elif path == "/api/releases":
            self._json({"version": int(get_meta("version", "0")), "status": self.watcher.status,
                        "releases": release_rows()})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._local_only():
            return
        path = urlsplit(self.path).path
        body = self._body()
        m = re.fullmatch(r"/api/releases/(\d+)", path)
        if m:
            status = body.get("status")
            if status not in ("new", "saved", "dismissed"):
                return self._json({"error": "bad status"}, 400)
            with _db_lock, db() as conn:
                conn.execute("UPDATE releases SET status=?, status_changed=? WHERE id=?", (status, now_iso(), int(m.group(1))))
            bump_version()
            return self._json({"ok": True})
        if path == "/api/check-now":
            self.watcher.wake.set()
            return self._json({"ok": True})
        if path == "/api/rescrape":
            rid = int(body.get("id", 0))
            with _db_lock, db() as conn:
                row = conn.execute("SELECT url FROM releases WHERE id=?", (rid,)).fetchone()
            if not row:
                return self._json({"error": "not found"}, 404)
            try:
                store_scrape(rid, scrape_release(row["url"]))
                bump_version()
                return self._json({"ok": True})
            except Exception as exc:
                return self._json({"error": str(exc)}, 502)
        self._json({"error": "not found"}, 404)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    init_db()
    watcher = GmailWatcher(cfg)
    Handler.watcher = watcher
    Handler.cfg = cfg
    threading.Thread(target=watcher.run, daemon=True).start()
    server = ThreadingHTTPServer((cfg["host"], cfg["port"]), Handler)
    log.info("Bandcamp Release Scanner on http://%s:%d", cfg["host"], cfg["port"])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())

# Bandcamp Release Scanner

Screens new Bandcamp releases from your Gmail. Every "New release from …" email from
`noreply@bandcamp.com` becomes a card with the Bandcamp player, the real release date
(pre-orders and future dates called out), and any vinyl or merch highlighted.

Python standard library only: no dependencies.

## How it works

- `scanner.py` checks Gmail over IMAP every 60 seconds, searching All Mail so filtered or archived
  emails still count. Each check searches from the previous successful one, so downtime never loses
  emails. The very first run backfills `BACKFILL_DAYS` (default 2).
- Once an email has been added it is marked as read and given the Gmail label `bandcamp_parsed`.
  Emails that couldn't be parsed are left unread so you notice them.
- It fetches each linked release page and reads Bandcamp's embedded release data: album/track ID (for
  the player), release date, pre-order status, tracks, tags, and physical packages (vinyl, CD, cassette,
  or merch like T-shirts, with prices, ship dates and sold-out status).
- Emails for the same release within 48 hours are merged into one card. A later email (e.g. release
  day after a pre-order announcement) creates a new card marked "Seen before".
- Upcoming releases are re-checked every 12 hours in case the date or formats change.
- New releases send a notification through Gotify (or a macOS notification when run on a Mac).

## Using it

| Tab        | Meaning                                                                                  |
|------------|------------------------------------------------------------------------------------------|
| To screen  | New releases you haven't dealt with                                                      |
| Want list  | Saved releases. **Open all** opens every page in new tabs; **Remove** moves one to Listened |
| Listened   | Dismissed                                                                                |

**Open all** opens tabs from your browser, which blocks all but one pop-up per click until you allow
pop-ups for the site (icon at the right of Chrome's address bar). The page tells you when that happens.

Keyboard: `j`/`k` move, `s` save, `d` dismiss, `o` open page, `z` undo.
Filters: Vinyl, Merch, Upcoming, Out now; sort by email date or release date.

## Deploying with Docker

The image serves on port 8765 and keeps its SQLite database in `/data`. See
[`docker-compose.example.yml`](docker-compose.example.yml) for a Traefik service with forward auth.
Put the code in a checkout that the compose `build:` points at, then:

```bash
git -C /path/to/checkout pull --ff-only
docker compose up -d --build bandcamp-scanner
```

The page has no login of its own; put it behind an authenticating proxy if it is reachable from a network.

## Settings

Environment variables (or keys in an optional `config.json` beside `scanner.py`):

| Variable                | Default            | Meaning                                                       |
|-------------------------|--------------------|---------------------------------------------------------------|
| `GMAIL_ADDRESS`         |                    | Gmail account to watch                                         |
| `GMAIL_APP_PASSWORD`    |                    | Gmail app password (https://myaccount.google.com/apppasswords); IMAP must be enabled |
| `POLL_SECONDS`          | 60                 | How often to check Gmail                                       |
| `BACKFILL_DAYS`         | 2                  | How far back the very first run looks                          |
| `MARK_READ`             | true               | Mark added emails as read                                      |
| `GMAIL_LABEL`           | bandcamp_parsed    | Label for added emails (empty to skip)                         |
| `NOTIFY`                | true               | Send notifications for new releases                            |
| `GOTIFY_URL`            |                    | Gotify server, e.g. `http://gotify:24045`                      |
| `GOTIFY_TOKEN`          |                    | Gotify application token                                       |
| `GOTIFY_PRIORITY`       | 5                  | Gotify message priority                                        |
| `PUBLIC_URL`            |                    | Link opened when a notification is clicked                     |
| `BCS_HOST` / `BCS_PORT` | 127.0.0.1 / 8765   | Listen address (the Docker image uses 0.0.0.0)                 |
| `BCS_DB`                | `releases.db`      | Database path (the Docker image uses `/data/releases.db`)      |

## Running on a Mac instead

`./setup.sh` installs it as a LaunchAgent, storing the app password in the macOS Keychain;
`./uninstall.sh` removes it.

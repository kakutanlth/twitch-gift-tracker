"""
Pandathon tracker
=================
Listens to one Twitch channel's chat (read-only, no Twitch login needed) and adds
every sub, resub, Prime sub, gifted sub and bits cheer to the "Points" Google Sheet.

  Column A  Name          written once, when a person first appears
  Column B  Tier 1 Subs   (Prime subs count as Tier 1)
  Column C  Tier 2 Subs
  Column D  Tier 3 Subs
  Column E  Bits          from cheers of at least min_cheer_bits (100) in one message
  Column F  Dollars       Ko-fi and Streamlabs donations, only when those are switched on in config.json
  Column I  Bits          from smaller cheers ("<100 Bits cheers")
  Columns G, H and J onwards (Bags, Total, Total cheer) are never touched.

Usage (normally through run_tracker.bat):
  python pandathon_tracker.py               run for real
  python pandathon_tracker.py --dry-run     only print what chat shows; nothing is saved or written
  python pandathon_tracker.py --test-sheet  write one test row to the sheet, to check the Google setup
Or double-click run_tracker_gui.bat for a window with Start/Stop, Refresh, Open sheet and settings.

Settings live in config.json next to this file.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import websocket  # pip package "websocket-client"

BASE = Path(__file__).resolve().parent
CHAT_URL = "wss://irc-ws.chat.twitch.tv:443"

FIELDS = ("t1", "t2", "t3", "bits", "small_bits", "dollars")
COLUMN = {"t1": "B", "t2": "C", "t3": "D", "bits": "E", "small_bits": "I", "dollars": "F"}
MONEY_COLUMNS = {"F"}  # Dollars keeps cents; every other column is a whole count
COLUMN_INDEX = {column: index for index, column in enumerate("ABCDEFGHI")}
DATA_COLUMNS = "ABCDEFI"  # the columns holding a person's data; G, H and J are formulas
TIER_BY_PLAN = {"Prime": "t1", "1000": "t1", "2000": "t2", "3000": "t3"}
TIER_LABEL = {"t1": "Tier 1", "t2": "Tier 2", "t3": "Tier 3"}

ANON_KEY = "*anonymous*"
ANON_LOGINS = {"ananonymousgifter", "ananonymouscheerer"}
# Chat notices that mention subs but are not a new payment today, so they are not counted.
NOT_COUNTED = {
    "giftpaidupgrade": "promised to continue a gifted sub",
    "anongiftpaidupgrade": "promised to continue a gifted sub",
    "primepaidupgrade": "converted a Prime sub to a paid sub",
    "standardpayforward": "is paying a gift forward (the gift itself is counted separately)",
    "communitypayforward": "is paying a gift forward (the gift itself is counted separately)",
}

log = logging.getLogger("tracker")


# --------------------------------------------------------------------------- config

DEFAULT_CONFIG = {
    "channel": "",
    "spreadsheet_id": "",
    "worksheet": "Points",
    "credentials_file": "service_account.json",
    "first_data_row": 3,
    "last_data_row": 250,
    "anonymous_row": 2,
    "anonymous_name": "Anonymous",
    "min_cheer_bits": 100,
    "flush_seconds": 3,
    # Donations (both off until filled in): see "Ko-fi and Streamlabs donations" in README.md
    "streamlabs_socket_token": "",
    "kofi_tab": "",
    "kofi_types": ["Donation", "Subscription"],
    "kofi_check_seconds": 20,
    "currency_rates": {"USD": 1},
}


def load_config() -> dict:
    path = BASE / "config.json"
    if not path.exists():
        sys.exit(f"config.json is missing (expected at {path}). Copy config.example.json to config.json "
                 "(the launchers do this on first run).")
    try:
        cfg = {**DEFAULT_CONFIG, **json.loads(path.read_text(encoding="utf-8-sig"))}
    except json.JSONDecodeError as exc:
        sys.exit(f"config.json is not valid JSON: {exc}")
    cfg["channel"] = clean_channel(cfg["channel"])
    if not row_layout(cfg)[2]:
        sys.exit('In config.json, "first_data_row" to "last_data_row" leaves no rows for people.')
    return cfg


def spreadsheet_id_from(text: str) -> str:
    """The spreadsheet ID from a Google Sheets link (or the bare ID); '' if it is neither."""
    text = (text or "").strip()
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", text)
    if match:
        return match.group(1)
    return text if re.fullmatch(r"[A-Za-z0-9_-]{25,}", text) else ""


def sheet_url(spreadsheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit" if spreadsheet_id else ""


def row_layout(cfg: dict):
    """(top row, bottom row, rows for people). The anonymous row may sit above, below or among them."""
    first, last, anon = cfg["first_data_row"], cfg["last_data_row"], cfg["anonymous_row"]
    people = [row for row in range(first, last + 1) if row != anon]
    return min(first, anon), max(last, anon), people


def clean_channel(value: str) -> str:
    """Accepts 'Name', '#name' or 'https://www.twitch.tv/name'."""
    value = (value or "").strip().rstrip("/")
    value = value.rsplit("/", 1)[-1]
    return value.lstrip("#@").lower()


# --------------------------------------------------------------------------- Twitch chat parsing

_TAG_ESCAPES = {"\\s": " ", "\\:": ";", "\\\\": "\\", "\\r": "\r", "\\n": "\n"}
_TAG_ESCAPE_RE = re.compile(r"\\[s:\\rn]")


def parse_irc(line: str):
    """Split one raw chat line into (tags, prefix, command, trailing text)."""
    tags = {}
    if line.startswith("@"):
        raw, _, line = line[1:].partition(" ")
        for part in raw.split(";"):
            key, _, value = part.partition("=")
            tags[key] = _TAG_ESCAPE_RE.sub(lambda m: _TAG_ESCAPES[m.group(0)], value)
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    head, _, trailing = line.partition(" :")
    command = head.split(" ", 1)[0]
    return tags, prefix, command, trailing


def to_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def sheet_name(display: str, login: str) -> str:
    """Display name; if it is not just the login in different capitals, add the login too."""
    display = (display or "").strip()
    if not display or display.lower() == login:
        return display or login
    return f"{display} ({login})"


class EventExtractor:
    """Turns chat lines into countable events and makes sure gift bundles are counted once.

    A "5 gifted subs" bundle arrives as one summary line plus five individual gift lines that
    all share a gift id. Whatever order they arrive in, and even if some are missed, the
    bundle is credited max(summary count, individual lines seen), never the sum of both.
    A cheer message with fewer than min_bits bits in total counts as small_bits (column I), not bits.
    """

    def __init__(self, min_bits: int = 1):
        self.min_bits = max(1, min_bits)
        self._bundles = {}  # gift id -> [credited, individual lines seen, last activity]
        self._legacy = {}   # (login, tier) -> [remaining, expiry]; bundles with no gift id

    def extract(self, tags: dict, prefix: str, command: str):
        source_room = tags.get("source-room-id")
        if source_room and source_room != tags.get("room-id"):
            return None  # Shared Chat: this happened in the other streamer's channel

        if command == "PRIVMSG":
            bits = to_int(tags.get("bits"))
            if bits <= 0:
                return None
            login = prefix.split("!", 1)[0].lower()
            field = "bits" if bits >= self.min_bits else "small_bits"
            return self._event(tags, login, "cheer", field, bits)

        if command != "USERNOTICE":
            return None
        kind = tags.get("msg-id", "")
        login = (tags.get("login") or "").lower()
        if kind in NOT_COUNTED:
            log.info("(not counted) %s %s", tags.get("display-name") or login, NOT_COUNTED[kind])
            return None
        if kind not in ("sub", "resub", "subgift", "anonsubgift", "submysterygift", "anonsubmysterygift"):
            return None

        plan = tags.get("msg-param-sub-plan", "")
        tier = TIER_BY_PLAN.get(plan)
        if tier is None:
            log.warning("Unknown sub plan %r from %s; counting it as Tier 1", plan, login)
            tier = "t1"
        if kind.startswith("anon"):
            login = "ananonymousgifter"

        if kind in ("sub", "resub"):
            count = 1
        elif kind in ("subgift", "anonsubgift"):
            count = self._single_gift(tags, login, tier)
        else:
            count = self._bundle_summary(tags, login, tier)
        if count <= 0:
            return None
        return self._event(tags, login, kind, tier, count)

    def _bundle_summary(self, tags, login, tier) -> int:
        count = to_int(tags.get("msg-param-mass-gift-count"), 1)
        gift_id = tags.get("msg-param-community-gift-id") or tags.get("msg-param-origin-id")
        now = time.monotonic()
        self._prune(now)
        if not gift_id:
            entry = self._legacy.setdefault((login, tier), [0, 0.0])
            entry[0] += count
            entry[1] = now + 600
            return count
        bundle = self._bundles.setdefault(gift_id, [0, 0, now])
        extra = max(0, count - bundle[0])
        bundle[0] += extra
        bundle[2] = now
        return extra

    def _single_gift(self, tags, login, tier) -> int:
        gift_id = tags.get("msg-param-community-gift-id")
        now = time.monotonic()
        if gift_id:
            bundle = self._bundles.setdefault(gift_id, [0, 0, now])
            bundle[1] += 1
            bundle[2] = now
            if bundle[1] > bundle[0]:
                bundle[0] += 1
                return 1
            return 0  # already credited by the bundle summary
        entry = self._legacy.get((login, tier))
        if entry and entry[0] > 0 and now < entry[1]:
            entry[0] -= 1
            return 0
        return 1

    def _prune(self, now):
        if len(self._bundles) > 500:
            self._bundles = {k: v for k, v in self._bundles.items() if now - v[2] < 3600}
        if len(self._legacy) > 500:
            self._legacy = {k: v for k, v in self._legacy.items() if now < v[1]}

    @staticmethod
    def _event(tags, login, kind, field, amount) -> dict:
        anon = login in ANON_LOGINS or not login
        event = {
            "id": tags.get("id") or "",
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "kind": kind,
            "key": ANON_KEY if anon else login,
            "name": "" if anon else sheet_name(tags.get("display-name", ""), login),
            "t1": 0, "t2": 0, "t3": 0, "bits": 0, "small_bits": 0, "dollars": 0,
        }
        event[field] = amount
        return event


def describe(event: dict) -> str:
    who = event["name"] or "Anonymous"
    if event.get("dollars"):
        source = {"kofi": "Ko-fi", "streamlabs": "Streamlabs"}.get(event["kind"], event["kind"])
        return f"DONATE {event['dollars']:>8.2f}  {who} ({source})"
    if event["bits"]:
        return f"BITS  {event['bits']:>6}        {who}"
    if event.get("small_bits"):
        return f"BITS  {event['small_bits']:>6} small  {who}"
    for tier in ("t1", "t2", "t3"):
        if event[tier]:
            label = {"sub": "SUB", "resub": "RESUB"}.get(event["kind"], "GIFT")
            return f"{label:<5} {TIER_LABEL[tier]} x{event[tier]:<4} {who}"
    return f"{event['kind']} {who}"


# --------------------------------------------------------------------------- donations (Streamlabs and Ko-fi)

KOFI_HEADERS = ["Received", "Ko-fi time", "Type", "Name", "Amount", "Currency", "Message", "Public",
                "Transaction ID", "Message ID", "Tracker"]  # the Ko-fi tab kofi_webhook.gs fills (its
                                                             # Forwarded column comes after these and is not read)


def sheet_amount(amount, currency, cfg: dict):
    """A donation in the sheet's money (Dollars), using currency_rates; None if that currency has no rate."""
    try:
        value = float(str(amount).replace(",", ""))
    except ValueError:
        return None
    rates = {str(code).strip().upper(): rate for code, rate in (cfg.get("currency_rates") or {}).items()}
    rate = rates.get(str(currency or "").strip().upper())
    return None if rate is None else round(value * float(rate), 2)


def donation_event(source: str, donation_id: str, name: str, dollars: float) -> dict:
    """A Ko-fi or Streamlabs donation as a tracker event, credited to Dollars (column F).

    Donors type their own name, so it is matched against the Name column like a Twitch name, ignoring capitals.
    """
    name = str(name or "").strip()
    anon = not name or name.lower() == "anonymous"
    return {
        "id": f"{source}:{donation_id}",
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "kind": source,
        "key": ANON_KEY if anon else Syncer._key_of_name(name),
        "name": "" if anon else name,
        "t1": 0, "t2": 0, "t3": 0, "bits": 0, "small_bits": 0, "dollars": dollars,
    }


def streamlabs_events(payload, cfg: dict) -> list:
    """The donations in one Streamlabs socket message, each as (event, None) or (None, why it is not counted)."""
    if not isinstance(payload, dict) or payload.get("type") != "donation":
        return []  # follows, subs and bits: the tracker counts those from chat
    found = []
    for item in payload.get("message") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("from") or ""
        label = f"{name or 'Anonymous'}, {item.get('amount')} {item.get('currency') or ''}".strip()
        donation_id = item.get("id") or item.get("_id")
        dollars = sheet_amount(item.get("amount"), item.get("currency"), cfg)
        if item.get("isTest") or item.get("is_test"):
            found.append((None, f"test alert ({label})"))
        elif not donation_id:
            found.append((None, f"no donation ID ({label})"))
        elif dollars is None:
            found.append((None, f"no rate for {item.get('currency')} in currency_rates ({label})"))
        elif dollars <= 0:
            found.append((None, f"zero amount ({label})"))
        else:
            found.append((donation_event("streamlabs", str(donation_id), name, dollars), None))
    return found


def kofi_event(values, cfg: dict):
    """One row of the Ko-fi tab -> (event or None, the text for its Tracker column)."""
    values = list(values) + [""] * (len(KOFI_HEADERS) - len(values))
    _, _, kind, name, amount, currency, _, public, _, message_id = values[:10]
    kind, message_id = str(kind).strip(), str(message_id).strip()
    if not message_id:
        return None, "not counted: no message ID"
    if kind.lower() not in {str(k).strip().lower() for k in cfg.get("kofi_types") or []}:
        return None, f"not counted: {kind or 'unknown type'}"
    dollars = sheet_amount(amount, currency, cfg)
    if dollars is None:
        return None, f"not counted: no rate for {currency or 'unknown currency'} in currency_rates"
    if dollars <= 0:
        return None, "not counted: zero amount"
    if str(public).strip().lower() == "no":
        name = ""  # a private donation: credited to the Anonymous row
    return donation_event("kofi", message_id, name, dollars), "added"


# --------------------------------------------------------------------------- local event log

class Store:
    """Every counted event is appended to data/events.jsonl before anything else happens.

    data/state.json remembers how far the sheet has been brought up to date, so a restart
    (or a crash) continues where it stopped instead of losing or double-counting events.
    With folder=None everything stays in memory (used by --test-sheet and the self-tests).
    """

    def __init__(self, folder: Path | None):
        self._lock = threading.Lock()
        self._folder = folder
        self._events = []       # events with seq > applied_seq
        self._seen_ids = set()
        self._seq = 0
        self.applied_seq = 0
        self.key_applied = {}   # key -> seq: this person's events up to seq are already in the sheet
        self.inflight = None    # the last write whose outcome is not confirmed yet
        if folder:
            folder.mkdir(parents=True, exist_ok=True)
            self._load()

    def _load(self):
        state_path = self._folder / "state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.applied_seq = state.get("applied_seq", 0)
            self.key_applied = state.get("key_applied", {})
            self.inflight = state.get("inflight")
        log_path = self._folder / "events.jsonl"
        if log_path.exists():
            with log_path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # a half-written last line after a crash
                    self._seq = max(self._seq, event["seq"])
                    if event.get("id"):
                        self._seen_ids.add(event["id"])
                    if event["seq"] > self.applied_seq:
                        self._events.append(event)
        self._seq = max(self._seq, self.applied_seq)

    def _save_state(self):
        if not self._folder:
            return
        state = {"applied_seq": self.applied_seq, "key_applied": self.key_applied, "inflight": self.inflight}
        tmp = self._folder / "state.json.tmp"
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, self._folder / "state.json")

    def add(self, event: dict) -> bool:
        with self._lock:
            if event.get("id") and event["id"] in self._seen_ids:
                return False
            self._seq += 1
            event = {"seq": self._seq, **event}
            if self._folder:
                with (self._folder / "events.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            if event.get("id"):
                self._seen_ids.add(event["id"])
            self._events.append(event)
            return True

    def pending(self) -> list:
        with self._lock:
            return [e for e in self._events if e["seq"] > self.key_applied.get(e["key"], 0)]

    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def resolve_inflight(self, confirmed_keys):
        """The unconfirmed write has been checked against the sheet; remember which rows made it."""
        with self._lock:
            for key in confirmed_keys:
                self.key_applied[key] = self.inflight["upto_seq"]
            self.inflight = None
            self._save_state()

    def set_inflight(self, upto_seq: int, expect: dict):
        with self._lock:
            self.inflight = {"upto_seq": upto_seq, "rows": expect}
            self._save_state()

    def mark_applied(self, upto_seq: int):
        with self._lock:
            self.applied_seq = upto_seq
            self.inflight = None
            self.key_applied = {}
            self._events = [e for e in self._events if e["seq"] > upto_seq]
            self._save_state()


# --------------------------------------------------------------------------- Google Sheet

class SheetError(Exception):
    pass


def sheets_api_off(exc: Exception) -> bool:
    """True when Google refused because the Sheets API is switched off in the key's Cloud project.

    gspread turns every refusal into a bare PermissionError; Google's own reason is on the APIError behind it.
    """
    for candidate in (exc, exc.__cause__):
        error = getattr(candidate, "error", None)
        if isinstance(error, dict) and any(
            isinstance(d, dict) and d.get("reason") == "SERVICE_DISABLED" for d in error.get("details", [])
        ):
            return True
    return False


class GoogleSheet:
    def __init__(self, cfg: dict):
        import gspread  # imported here so --dry-run works before Google is set up

        cred_path = Path(cfg["credentials_file"])
        if not cred_path.is_absolute():
            cred_path = BASE / cred_path
        if not cred_path.exists():
            raise SheetError(
                f"Google key file not found: {cred_path}\n"
                "Download the service-account JSON key and save it there (see the setup steps)."
            )
        spreadsheet = None
        try:
            key = json.loads(cred_path.read_text(encoding="utf-8"))
            self.email = key.get("client_email", "?")
            client = gspread.service_account(filename=str(cred_path))
            client.set_timeout(30)
            spreadsheet = client.open_by_key(cfg["spreadsheet_id"])
            self._ws = spreadsheet.worksheet(cfg["worksheet"])
        except gspread.exceptions.WorksheetNotFound:
            tabs = ", ".join(repr(ws.title) for ws in spreadsheet.worksheets())
            raise SheetError(f"The spreadsheet has no tab called {cfg['worksheet']!r}. Its tabs are: {tabs}.")
        except (PermissionError, gspread.exceptions.SpreadsheetNotFound) as exc:
            if sheets_api_off(exc):
                project = key.get("project_id", "")
                raise SheetError(
                    f"The Google Sheets API is switched off in the Google Cloud project {project}.\n"
                    f"Turn it on at https://console.cloud.google.com/apis/library/sheets.googleapis.com?project={project}\n"
                    "then wait a few minutes and start the tracker again."
                )
            raise SheetError(
                f"Cannot open the spreadsheet. Share it with {self.email} as Editor, "
                'and check "spreadsheet_id" in config.json.'
            )
        except gspread.exceptions.APIError as exc:
            if exc.response.status_code == 429 or exc.response.status_code >= 500:
                raise  # Google is busy or having trouble: worth another try, not a setup mistake
            raise SheetError(
                f"Google refused the request: {exc}\n"
                f"- Is the sheet shared with {self.email} as Editor?\n"
                "- Is the Google Sheets API enabled in the Google Cloud project?"
            )
        self._top, self._bottom, _ = row_layout(cfg)
        if self._ws.row_count < self._bottom:
            raise SheetError(
                f"The sheet only has {self._ws.row_count} rows, but the tracker uses rows down to {self._bottom}.\n"
                f"Add rows down to at least {self._bottom} (and extend the Bags formula), "
                'or lower "last_data_row" / "anonymous_row" in config.json.'
            )

    @property
    def title(self) -> str:
        """The spreadsheet's own name, as shown at the top of Google Sheets."""
        return self._ws.spreadsheet.title

    def headers(self) -> list:
        return self._ws.row_values(1)

    def tab(self, name: str):
        """Another tab of the same spreadsheet (the Ko-fi tab)."""
        import gspread

        try:
            return self._ws.spreadsheet.worksheet(name)
        except gspread.exceptions.WorksheetNotFound:
            tabs = ", ".join(repr(ws.title) for ws in self._ws.spreadsheet.worksheets())
            raise SheetError(f"The spreadsheet has no tab called {name!r} for Ko-fi donations. Its tabs are: {tabs}.")

    def read(self) -> list:
        return self._ws.get(f"A{self._top}:I{self._bottom}", value_render_option="UNFORMATTED_VALUE")

    def write(self, data: list):
        self._ws.batch_update(data, value_input_option="RAW")


class Syncer:
    """Brings the sheet up to date with the local event log."""

    def __init__(self, store: Store, sheet, cfg: dict):
        self.store = store
        self.sheet = sheet
        self.top, _, self.people_rows = row_layout(cfg)
        self.anon_row = cfg["anonymous_row"]
        self.anon_name = cfg["anonymous_name"]
        self._flush_lock = threading.Lock()

    # -- helpers for the grid returned by sheet.read() (rows of columns A..I from the top row, possibly ragged)
    def _cell(self, grid, row, column):
        r, c = row - self.top, COLUMN_INDEX[column]
        if 0 <= r < len(grid) and c < len(grid[r]):
            return grid[r][c]
        return ""

    def _number(self, grid, row, column):
        """A cell as the tracker counts it: a whole number, or money to the cent in Dollars."""
        value = self._cell(grid, row, column)
        if value in ("", None):
            return 0
        try:
            number = float(str(value).replace(",", "").lstrip("$"))
            return round(number, 2) if column in MONEY_COLUMNS else int(round(number))
        except ValueError:
            log.warning("Cell %s%d contains %r, not a number; treating it as 0", column, row, value)
            return 0

    @staticmethod
    def _key_of_name(text) -> str:
        text = str(text).strip()
        match = re.search(r"\(([A-Za-z0-9_]+)\)$", text)  # "表示名 (login)"
        return (match.group(1) if match else text).lower()

    def _index(self, grid):
        rows_by_key, free_rows = {}, []
        for row in self.people_rows:
            name = str(self._cell(grid, row, "A")).strip()
            if name:
                rows_by_key.setdefault(self._key_of_name(name), row)
            elif all(self._cell(grid, row, col) in ("", None) for col in "BCDEFI"):
                free_rows.append(row)
        return rows_by_key, free_rows

    def flush(self) -> int:
        """Write all pending events. Returns how many events were written. Raises on failure."""
        with self._flush_lock:  # never two sheet updates at the same time
            return self._flush()

    def _flush(self) -> int:
        upto = self.store.last_seq()
        if upto <= self.store.applied_seq:
            return 0
        grid = self.sheet.read()
        rows_by_key, free_rows = self._index(grid)

        # A previous write may have reached Google even though we never got the answer.
        # Compare what that write would have produced with what the sheet shows now.
        inflight = self.store.inflight
        if inflight:
            confirmed = []
            for key, expect in inflight["rows"].items():
                row = self.anon_row if key == ANON_KEY else rows_by_key.get(key)
                if row and all(self._number(grid, row, col) == value for col, value in expect.items()):
                    confirmed.append(key)
            self.store.resolve_inflight(confirmed)

        pending = [e for e in self.store.pending() if e["seq"] <= upto]
        if not pending:
            self.store.mark_applied(upto)
            return 0

        totals = {}
        for event in pending:
            entry = totals.setdefault(event["key"], {"name": event["name"], **{field: 0 for field in FIELDS}})
            for field in FIELDS:
                entry[field] += event.get(field, 0)  # events saved before a column existed lack that field

        data, expect = [], {}
        for key, entry in totals.items():
            if key == ANON_KEY:
                row = self.anon_row
                if not str(self._cell(grid, row, "A")).strip():
                    data.append({"range": f"A{row}", "values": [[self.anon_name]]})
            else:
                row = rows_by_key.get(key)
                if row is None:
                    if not free_rows:
                        raise SheetError(
                            f"No empty row left for new people in rows {self.people_rows[0]} to {self.people_rows[-1]}. "
                            "Nothing is lost: free up rows and the tracker will catch up by itself."
                        )
                    row = free_rows.pop(0)
                    data.append({"range": f"A{row}", "values": [[entry["name"]]]})
            expect[key] = {}
            for field in FIELDS:
                if entry[field]:
                    value = self._number(grid, row, COLUMN[field]) + entry[field]
                    if COLUMN[field] in MONEY_COLUMNS:
                        value = round(value, 2)
                    expect[key][COLUMN[field]] = value
                    data.append({"range": f"{COLUMN[field]}{row}", "values": [[value]]})

        self.store.set_inflight(upto, expect)
        self.sheet.write(data)
        self.store.mark_applied(upto)
        return len(pending)

    # -- deleting a person's row (the window's Delete row button)

    def _names_of(self, text) -> set:
        """The ways a Name cell can be typed: in full, the login in brackets, or the display name before it."""
        text = str(text).strip()
        if not text:
            return set()
        display = re.sub(r"\s*\([A-Za-z0-9_]+\)$", "", text)
        return {text.lower(), self._key_of_name(text), display.lower()}

    def _row_data(self, grid, row) -> list:
        return [self._cell(grid, row, column) for column in DATA_COLUMNS]

    def _used_rows_from(self, grid, row) -> list:
        """People rows from this one down to the last row holding any data."""
        below = self.people_rows[self.people_rows.index(row):]
        used = [r for r in below if any(v not in ("", None) for v in self._row_data(grid, r))]
        return below[: below.index(used[-1]) + 1] if used else [row]

    def find_row(self, by: str, value: str) -> dict:
        """Find the row to delete, by "row" number or by user "name" (not case sensitive).

        Returns {"row", "name", "data", "rows_below"}; raises SheetError when that row cannot be deleted.
        """
        grid = self.sheet.read()
        value = str(value).strip()
        if by == "row":
            row = to_int(value, 0)
            if row == self.anon_row:
                raise SheetError(f"Row {row} is the {self.anon_name} row, which the tracker needs. It cannot be deleted.")
            if row not in self.people_rows:
                raise SheetError(f"Row {value} is not one of the people rows ({self.people_rows[0]} to {self.people_rows[-1]}).")
        else:
            wanted = value.lower()
            matches = [r for r in self.people_rows if wanted in self._names_of(self._cell(grid, r, "A"))]
            if not matches:
                if wanted == self.anon_name.lower():
                    raise SheetError(f"The {self.anon_name} row is needed by the tracker. It cannot be deleted.")
                raise SheetError(f"No row has the username {value!r}.")
            if len(matches) > 1:
                rows = ", ".join(map(str, matches))
                raise SheetError(f"{len(matches)} rows match {value!r} (rows {rows}). Delete one of them by row number.")
            row = matches[0]
        data = self._row_data(grid, row)
        if all(v in ("", None) for v in data):
            raise SheetError(f"Row {row} is already empty.")
        return {"row": row, "name": str(data[0]).strip(), "data": dict(zip(DATA_COLUMNS, data)),
                "rows_below": len(self._used_rows_from(grid, row)) - 1}

    def delete_row(self, row: int, expected_name: str) -> list:
        """Empty one person's row and move the people rows below it up by one. Returns the rows rewritten.

        Only the data columns (A-F and I) move; the formula columns (Bags, Total, Total cheer) stay where they
        are and recalculate. Waits for any sheet update in progress, and refuses if the row changed since find_row().
        """
        with self._flush_lock:
            grid = self.sheet.read()
            if row not in self.people_rows:
                raise SheetError(f"Row {row} is not one of the people rows.")
            current = str(self._cell(grid, row, "A")).strip()
            if current != expected_name:
                raise SheetError(f"Row {row} changed since you chose it (its name is now {current or '(empty)'!r}). "
                                 "Nothing was deleted.")
            rows = self._used_rows_from(grid, row)
            data = []
            for target, source in zip(rows, rows[1:] + [None]):
                values = self._row_data(grid, source) if source else [""] * len(DATA_COLUMNS)
                values = ["" if v is None else v for v in values]
                data.append({"range": f"A{target}:F{target}", "values": [values[:6]]})
                data.append({"range": f"I{target}", "values": [values[6:]]})
            self.sheet.write(data)
            return rows


# --------------------------------------------------------------------------- chat connection

class ReconnectRequested(Exception):
    pass


def run_chat(channel: str, on_message, stop: threading.Event, reconnect: threading.Event | None = None, on_state=None):
    """Stay connected to the channel's chat until stop is set, reconnecting whenever the line drops.

    Setting reconnect drops the connection and opens a new one (the window's Refresh button).
    on_state, if given, is called with "connecting", "connected" or "stopped".
    """
    def state(value):
        if on_state:
            on_state(value)

    backoff = 1.0
    while not stop.is_set():
        ws = None
        state("connecting")
        if reconnect is not None:
            reconnect.clear()
        try:
            ws = websocket.create_connection(CHAT_URL, timeout=20)
            ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            ws.send(f"NICK justinfan{random.randint(10000, 99999)}")
            ws.send(f"JOIN #{channel}")
            ws.settimeout(5)
            connected_at = last_rx = time.monotonic()
            joined = warned = pinged = False
            while not stop.is_set():
                if reconnect is not None and reconnect.is_set():
                    backoff = 1.0
                    raise ReconnectRequested()
                try:
                    data = ws.recv()
                except websocket.WebSocketTimeoutException:
                    now = time.monotonic()
                    if not joined and not warned and now - connected_at > 20:
                        warned = True
                        log.warning("Twitch has not confirmed joining #%s. Is the channel name right?", channel)
                    quiet = now - last_rx
                    if quiet > 90:
                        raise ConnectionError("chat went silent")
                    if quiet > 45 and not pinged:
                        ws.send("PING :tracker")
                        pinged = True
                    continue
                if not data:
                    raise ConnectionError("Twitch closed the connection")
                last_rx = time.monotonic()
                pinged = False
                if isinstance(data, bytes):
                    data = data.decode("utf-8", "replace")
                for line in data.split("\r\n"):
                    if not line:
                        continue
                    tags, prefix, command, trailing = parse_irc(line)
                    if command == "PING":
                        ws.send("PONG :" + trailing)
                    elif command == "RECONNECT":
                        raise ConnectionError("Twitch asked for a reconnect")
                    elif command == "ROOMSTATE" and not joined:
                        joined = True
                        backoff = 1.0
                        state("connected")
                        log.info("Connected. Listening to #%s ...", channel)
                    elif command == "NOTICE":
                        log.warning("Twitch says: %s", trailing)
                    elif command in ("PRIVMSG", "USERNOTICE"):
                        try:
                            on_message(tags, prefix, command)
                        except Exception:
                            log.exception("Could not process this chat line: %s", line)
        except ReconnectRequested:
            log.info("Reconnecting to chat ...")
        except Exception as exc:
            if not stop.is_set():
                log.warning("Chat connection lost (%s). Reconnecting in about %.0f s.", exc, backoff)
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
        (reconnect or stop).wait(backoff + random.random())  # Refresh or Stop cuts the wait short
        backoff = min(backoff * 2, 30)
    state("stopped")


def run_flusher(syncer: Syncer, store: Store, wake: threading.Event, stop: threading.Event, delay: float,
                retry_now: threading.Event | None = None):
    failures = 0
    while not stop.is_set():
        wake.wait(timeout=30)
        wake.clear()
        if stop.is_set():
            break
        stop.wait(delay)  # let a burst (a gift bundle, a hype train) collect into one write
        try:
            written = syncer.flush()
            if written:
                log.info("Sheet updated (%d event%s).", written, "" if written == 1 else "s")
            failures = 0
        except Exception as exc:
            failures += 1
            wait = min(60, 5 * 2 ** (failures - 1))
            log.warning("Could not update the sheet (%s). Events are saved; retrying in %d s.", exc, wait)
            (retry_now or stop).wait(wait)  # Refresh or Stop cuts the wait short
            if retry_now is not None:
                retry_now.clear()
            wake.set()


STREAMLABS_URL = "wss://sockets.streamlabs.com/socket.io/?EIO=3&transport=websocket&token={token}"


class StreamlabsRefused(Exception):
    pass


def run_streamlabs(token: str, on_event, stop: threading.Event, reconnect: threading.Event | None = None, on_state=None):
    """Stay connected to the Streamlabs Socket API until stop is set, reconnecting whenever the line drops.

    Streamlabs speaks Socket.IO 2 over a websocket: the client pings every pingInterval, and each alert arrives
    as 42["event", {...}]. on_event gets that {...}. Like chat, nothing is replayed after a disconnection.
    """
    def state(value):
        if on_state:
            on_state(value)

    url = STREAMLABS_URL.format(token=quote(token, safe=""))
    backoff = 1.0
    while not stop.is_set():
        ws, wait = None, None
        state("connecting")
        if reconnect is not None:
            reconnect.clear()
        try:
            ws = websocket.create_connection(url, timeout=20)
            greeting = ws.recv()
            if not str(greeting).startswith("0"):
                raise ConnectionError(f"unexpected greeting {str(greeting)[:60]!r}")
            info = json.loads(greeting[1:])
            ping_every = info.get("pingInterval", 25000) / 1000
            silent_limit = ping_every + info.get("pingTimeout", 20000) / 1000
            ws.settimeout(1)
            last_ping = last_rx = time.monotonic()
            while not stop.is_set():
                if reconnect is not None and reconnect.is_set():
                    backoff = 1.0
                    raise ReconnectRequested()
                now = time.monotonic()
                if now - last_ping >= ping_every:
                    ws.send("2")
                    last_ping = now
                if now - last_rx > silent_limit:
                    raise ConnectionError("Streamlabs went silent")
                try:
                    frame = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not frame:
                    raise ConnectionError("Streamlabs closed the connection")
                last_rx = time.monotonic()
                if frame == "40":
                    backoff = 1.0
                    state("connected")
                    log.info("Streamlabs connected. Listening for donations ...")
                elif frame.startswith("44"):
                    raise StreamlabsRefused(frame[2:].strip('"'))
                elif frame == "1" or frame.startswith("41"):
                    raise ConnectionError("Streamlabs ended the connection")
                elif frame.startswith("42"):
                    try:
                        name, *args = json.loads(frame[2:])
                        if name == "event" and args:
                            on_event(args[0])
                    except Exception:
                        log.exception("Could not process this Streamlabs message")
        except ReconnectRequested:
            log.info("Reconnecting to Streamlabs ...")
        except StreamlabsRefused as exc:
            wait = 300
            state("refused")
            log.error("Streamlabs refused the socket token (%s). Check streamlabs_socket_token in config.json. "
                      "Trying again in about 5 minutes.", exc)
        except Exception as exc:
            if not stop.is_set():
                log.warning("Streamlabs connection lost (%s). Reconnecting in about %.0f s.",
                            str(exc).replace(token, "***"), backoff)
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
        (reconnect or stop).wait((wait or backoff) + random.random())  # Refresh or Stop cuts the wait short
        backoff = min(backoff * 2, 60)
    state("stopped")


class KofiInbox:
    """The Ko-fi tab that kofi_webhook.gs fills. The tracker reads new rows and writes back what it did with each."""

    def __init__(self, sheet: GoogleSheet, tab: str):
        self._ws = sheet.tab(tab)

    def rows(self) -> list:
        """(row number, values) for every donation row, oldest first."""
        values = self._ws.get("A2:K", value_render_option="UNFORMATTED_VALUE")
        return [(n + 2, row) for n, row in enumerate(values) if any(str(v).strip() for v in row)]

    def mark(self, statuses: dict):
        if statuses:
            self._ws.batch_update([{"range": f"K{row}", "values": [[text]]} for row, text in statuses.items()],
                                  value_input_option="RAW")


def check_kofi(inbox, store: Store, cfg: dict) -> int:
    """Save each new row of the Ko-fi tab as an event, then fill in its Tracker column. Returns how many were new.

    A row whose Tracker column already says anything (including "skip", typed by hand) is left alone. Events are
    saved before the column is written, and their Ko-fi message ID stops them from ever being counted twice.
    """
    statuses, added = {}, 0
    for row, values in inbox.rows():
        if len(values) >= len(KOFI_HEADERS) and str(values[len(KOFI_HEADERS) - 1]).strip():
            continue
        event, status = kofi_event(values, cfg)
        if event is None:
            log.warning("Ko-fi tab row %d %s.", row, status)
        elif store.add(event):
            added += 1
            log.info(describe(event))
        statuses[row] = status
    inbox.mark(statuses)
    return added


def run_kofi(inbox, store: Store, cfg: dict, wake: threading.Event, stop: threading.Event,
             check_now: threading.Event, on_state=None):
    """Check the Ko-fi tab every kofi_check_seconds until stop is set."""
    every = max(5, to_int(cfg.get("kofi_check_seconds"), 20))
    while not stop.is_set():
        try:
            if check_kofi(inbox, store, cfg):
                wake.set()
            if on_state:
                on_state("ok")
        except Exception as exc:
            if on_state:
                on_state("error")
            log.warning("Could not read the Ko-fi tab (%s). Trying again in %d s.", exc, every)
        check_now.wait(every)  # Refresh or Stop cuts the wait short
        check_now.clear()
    if on_state:
        on_state("stopped")


# --------------------------------------------------------------------------- entry point

def setup_logging(to_file: bool):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    handlers = [logging.StreamHandler(sys.stdout)]
    if to_file:
        (BASE / "data").mkdir(exist_ok=True)
        handlers.append(logging.FileHandler(BASE / "data" / "tracker.log", encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S", handlers=handlers)


def test_sheet(cfg: dict):
    sheet = GoogleSheet(cfg)
    store = Store(None)
    store.add({"id": "", "kind": "sub", "key": "tracker_test", "name": "Tracker_Test", "t1": 1, "t2": 0, "t3": 0, "bits": 0})
    store.add({"id": "", "kind": "cheer", "key": "tracker_test", "name": "Tracker_Test", "t1": 0, "t2": 0, "t3": 0, "bits": 100})
    store.add({"id": "", "kind": "cheer", "key": "tracker_test", "name": "Tracker_Test", "t1": 0, "t2": 0, "t3": 0, "bits": 0, "small_bits": 50})
    Syncer(store, sheet, cfg).flush()
    print("Done. The sheet should now have a row named Tracker_Test with +1 Tier 1 sub, +100 bits and +50 in column I.")
    print("Run this again and it should become 2, 200 and 100. Clear that row before the event.")


def open_sheet(cfg: dict, stop: threading.Event):
    """Open the sheet at startup, waiting out a missing internet connection or a Google outage.

    Returns None if stop is set while waiting. Setup mistakes (key, sharing, tab, rows) raise SheetError,
    since waiting will not fix them.
    """
    failures = 0
    while not stop.is_set():
        try:
            return GoogleSheet(cfg)
        except SheetError:
            raise
        except Exception as exc:
            failures += 1
            wait = min(60, 5 * 2 ** (failures - 1))
            reason = str(exc)
            if not reason.startswith(type(exc).__name__):
                reason = f"{type(exc).__name__}: {reason}"
            log.warning("Could not reach Google (%s). Trying again in %d s.", reason, wait)
            stop.wait(wait)
    return None


class Tracker:
    """One tracking session: the chat listener plus the sheet writer. Used by main() and by the window."""

    def __init__(self, cfg: dict, dry_run: bool = False, data_folder: Path | None = None):
        self.cfg = cfg
        self.dry_run = dry_run
        self.data_folder = data_folder or BASE / "data"
        self.chat_state = "not started"
        self.streamlabs_state = "off"   # off / connecting / connected / refused / stopped
        self.kofi_state = "off"         # off / starting / ok / error / stopped
        self.store = None
        self.stopping = threading.Event()
        self._wake = threading.Event()        # update the sheet now: new events, or Refresh
        self._retry_now = threading.Event()   # cut short the wait after a failed sheet update
        self._reconnect = threading.Event()   # drop and reopen the chat connection
        self._streamlabs_reconnect = threading.Event()
        self._kofi_now = threading.Event()    # check the Ko-fi tab now
        self._extractor = EventExtractor(min_bits=to_int(cfg["min_cheer_bits"], 1))
        self._syncer = None
        self._threads = []

    def start(self) -> bool:
        """Open the sheet (waiting out network trouble), then start listening to chat.

        Returns False if request_stop() came first. Raises SheetError on a setup mistake.
        """
        if self.dry_run:
            log.info("DRY RUN: events are only printed. Nothing is saved or written to the sheet.")
        else:
            sheet = open_sheet(self.cfg, self.stopping)
            if sheet is None or self.stopping.is_set():
                return False
            self.store = Store(self.data_folder)
            self._syncer = Syncer(self.store, sheet, self.cfg)
            backlog = len(self.store.pending())
            if backlog:
                log.info("%d saved event(s) from last time are not in the sheet yet; writing them now.", backlog)
                self._wake.set()
            self._spawn(run_flusher, self._syncer, self.store, self._wake, self.stopping,
                        self.cfg["flush_seconds"], self._retry_now)
            kofi_tab = str(self.cfg.get("kofi_tab") or "").strip()
            if kofi_tab:
                try:
                    inbox = KofiInbox(sheet, kofi_tab)
                except Exception as exc:
                    log.error("Ko-fi donations are off: %s", exc)
                else:
                    self.kofi_state = "starting"
                    log.info("Ko-fi donations: checking the %r tab every %d s.", kofi_tab,
                             max(5, to_int(self.cfg.get("kofi_check_seconds"), 20)))
                    self._spawn(run_kofi, inbox, self.store, self.cfg, self._wake, self.stopping,
                                self._kofi_now, self._set_kofi_state)
        token = str(self.cfg.get("streamlabs_socket_token") or "").strip()
        if token:
            self.streamlabs_state = "connecting"
            self._spawn(run_streamlabs, token, self._on_streamlabs, self.stopping, self._streamlabs_reconnect,
                        self._set_streamlabs_state)
        self._spawn(run_chat, self.cfg["channel"], self._on_message, self.stopping, self._reconnect, self._set_chat_state)
        return True

    def _spawn(self, target, *args):
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _on_message(self, tags, prefix, command):
        event = self._extractor.extract(tags, prefix, command)
        if not event:
            return
        if self.store is None:
            log.info(describe(event))
        elif self.store.add(event):
            log.info(describe(event))
            self._wake.set()

    def _on_streamlabs(self, payload):
        for event, why in streamlabs_events(payload, self.cfg):
            if event is None:
                log.warning("Streamlabs donation not counted: %s.", why)
            elif self.store is None:
                log.info(describe(event))
            elif self.store.add(event):
                log.info(describe(event))
                self._wake.set()

    def _set_chat_state(self, state: str):
        self.chat_state = state

    def _set_streamlabs_state(self, state: str):
        self.streamlabs_state = state

    def _set_kofi_state(self, state: str):
        self.kofi_state = state

    @property
    def syncer(self):
        """The sheet writer while tracking (None in a dry run or before the sheet is open)."""
        return self._syncer

    def pending(self) -> int:
        """Events saved locally but not in the sheet yet."""
        return len(self.store.pending()) if self.store else 0

    def refresh(self):
        """Reconnect to chat (and Streamlabs), check the Ko-fi tab, and write any saved events to the sheet now."""
        log.info("Refresh: reconnecting and writing any saved events now.")
        self._reconnect.set()
        self._streamlabs_reconnect.set()
        self._kofi_now.set()
        self._retry_now.set()
        self._wake.set()

    def request_stop(self):
        """Ask every thread to stop without waiting for them."""
        self.stopping.set()
        self._wake.set()
        self._retry_now.set()
        self._reconnect.set()
        self._streamlabs_reconnect.set()
        self._kofi_now.set()

    def stop(self, timeout: float = 15):
        """Stop listening, write whatever is still waiting, and wait for every thread to finish."""
        self.request_stop()
        for thread in self._threads:
            thread.join(timeout)
        if self._syncer is not None:
            try:
                self._syncer.flush()
            except Exception as exc:
                log.warning("Final sheet update failed (%s).", exc)
            left = self.pending()
            if left:
                log.info("%d event(s) are saved but not in the sheet yet. They will be written next time you start.", left)
        log.info("Stopped.")


def main():
    parser = argparse.ArgumentParser(description="Twitch subs and bits -> Google Sheet")
    parser.add_argument("--dry-run", action="store_true", help="only print events; save and write nothing")
    parser.add_argument("--test-sheet", action="store_true", help="write one test row to the sheet and exit")
    parser.add_argument("--channel", help="override the channel from config.json")
    args = parser.parse_args()

    cfg = load_config()
    if args.channel:
        cfg["channel"] = clean_channel(args.channel)
    setup_logging(to_file=not (args.dry_run or args.test_sheet))

    if args.test_sheet:
        try:
            test_sheet(cfg)
        except SheetError as exc:
            sys.exit(f"\n{exc}")
        return

    if not cfg["channel"]:
        sys.exit('Set "channel" in config.json to the streamer\'s Twitch channel name first.')

    tracker = Tracker(cfg, dry_run=args.dry_run)
    try:
        if tracker.start():
            log.info("Press Ctrl+C to stop.")
            while True:
                time.sleep(0.5)
    except SheetError as exc:
        sys.exit(f"\n{exc}")
    except KeyboardInterrupt:
        pass
    tracker.stop()


if __name__ == "__main__":
    main()

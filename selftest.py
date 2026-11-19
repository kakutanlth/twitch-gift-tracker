"""Offline tests for the tracker: fake sheet, injected failures, gift-bundle de-duplication."""
import importlib.util, logging, shutil, sys, tempfile, threading, time
from pathlib import Path

spec = importlib.util.spec_from_file_location("tracker", sys.argv[1])
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)

CFG = {"first_data_row": 2, "last_data_row": 250, "anonymous_row": 250, "anonymous_name": "Anonymous"}
COLS = "ABCDEFGHI"


class FakeSheet:
    def __init__(self, cells=None):
        self.cells = dict(cells or {})  # "B7" -> value
        self.fail = None                # "before" / "after": raise around the next write
        self.writes = 0
        self.allowed = "ABCDEI"         # columns the tracker may write (deleting a row also moves F)

    def read(self):
        return [[self.cells.get(f"{c}{r}", "") for c in COLS] for r in range(2, 251)]

    def write(self, data):
        if self.fail == "before":
            self.fail = None
            raise TimeoutError("simulated: request never reached Google")
        for item in data:
            first, _, last = item["range"].partition(":")
            last = last or first
            assert first[1:] == last[1:], f"only single-row ranges are expected: {item}"
            cells = [f"{c}{first[1:]}" for c in COLS[COLS.index(first[0]):COLS.index(last[0]) + 1]]
            assert all(cell[0] in self.allowed for cell in cells), f"touched a forbidden column: {item}"
            assert len(cells) == len(item["values"][0]), item
            for cell, value in zip(cells, item["values"][0]):
                self.cells[cell] = value
        self.writes += 1
        if self.fail == "after":
            self.fail = None
            raise TimeoutError("simulated: Google applied it but the reply was lost")

    def row(self, r):
        return [self.cells.get(f"{c}{r}", "") for c in "ABCDE"]


def ev(key, name, **amounts):
    return {"id": "", "kind": "x", "key": key, "name": name, "t1": 0, "t2": 0, "t3": 0, "bits": 0, **amounts}


def check(label, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + label + ("" if ok else f"\n     got  {got}\n     want {want}"))
    return ok


results = []

# ---- 1. row placement, name matching, increments on top of existing values
sheet = FakeSheet({"A2": "Example", "B2": 10, "C2": 2, "D2": 1, "E2": 100, "F2": 10,
                   "F3": 5,                       # nameless Ko-fi row: must not be reused
                   "A4": "Panpire", "B4": 3, "E4": "1,200",
                   "A5": "熊猫 (panda_jp)", "D5": 2})
store = t.Store(None)
sync = t.Syncer(store, sheet, CFG)
store.add(ev("panpire", "Panpire", t1=2))
store.add(ev("panpire", "Panpire", bits=300))
store.add(ev("panda_jp", "熊猫 (panda_jp)", t3=1))
store.add(ev("newbie", "NewBie", t2=1))
store.add(ev(t.ANON_KEY, "", t1=5))
store.add(ev(t.ANON_KEY, "", bits=50))
sync.flush()
results += [
    check("existing row matched case-insensitively and incremented", sheet.row(4), ["Panpire", 5, "", "", 1500]),
    check("localised name matched by (login)", sheet.row(5), ["熊猫 (panda_jp)", "", "", 3, ""]),
    check("Example row untouched", sheet.row(2), ["Example", 10, 2, 1, 100]),
    check("Dollars-only row 3 not reused", sheet.row(3), ["", "", "", "", ""]),
    check("new person goes to first truly empty row (6)", sheet.row(6), ["NewBie", "", 1, "", ""]),
    check("anonymous goes to row 250", sheet.row(250), ["Anonymous", 5, "", "", 50]),
    check("Dollars column never written", (sheet.cells.get("F2"), sheet.cells.get("F3")), (10, 5)),
    check("nothing pending afterwards", len(store.pending()), 0),
]

# ---- 2. manual edit between events is respected
sheet.cells["B4"] = 100
store.add(ev("panpire", "Panpire", t1=1))
sync.flush()
results.append(check("increment applied on top of a manual edit", sheet.row(4)[1], 101))

# ---- 3. reply lost AFTER Google applied the write -> must not double count
sheet.fail = "after"
store.add(ev("panpire", "Panpire", bits=500))
store.add(ev("fresh", "Fresh", t1=1))
try:
    sync.flush(); results.append(check("lost reply raises", False, True))
except TimeoutError:
    pass
store.add(ev("panpire", "Panpire", bits=7))       # arrives while the outcome is unknown
store.add(ev("later", "Later", t1=1))
sync.flush()
results += [
    check("lost reply: bits counted once (+500 +7)", sheet.row(4)[4], 2007),
    check("lost reply: new person not duplicated", [r for r in range(2, 250) if sheet.row(r)[0] == "Fresh"], [7]),
    check("lost reply: Fresh has exactly 1 sub", sheet.row(7)[1], 1),
    check("later person lands on next row", sheet.row(8), ["Later", 1, "", "", ""]),
]

# ---- 4. request failed BEFORE reaching Google -> retried once
sheet.fail = "before"
store.add(ev("panpire", "Panpire", t2=4))
try:
    sync.flush(); results.append(check("failed write raises", False, True))
except TimeoutError:
    pass
sync.flush(); sync.flush()
results.append(check("failed write is retried exactly once", sheet.row(4)[2], 4))

# ---- 5. restart: events saved on disk are written after a restart, and only once
tmp = Path(tempfile.mkdtemp())
try:
    sheet2 = FakeSheet()
    s1 = t.Store(tmp)
    s1.add({**ev("alice", "Alice", t1=1), "id": "m1"})
    t.Syncer(s1, sheet2, CFG).flush()
    s1.add({**ev("alice", "Alice", bits=200), "id": "m2"})   # saved, then "crash" before any flush
    s1.add({**ev("bob", "Bob", t3=1), "id": "m3"})
    sheet2.fail = "after"                                     # and a lost reply right before the crash
    try:
        t.Syncer(s1, sheet2, CFG).flush()
    except TimeoutError:
        pass
    s2 = t.Store(tmp)                                         # fresh start from the files
    results.append(check("restart: duplicate message id rejected", s2.add({**ev("bob", "Bob", t3=1), "id": "m3"}), False))
    s2.add({**ev("bob", "Bob", t1=1), "id": "m4"})
    t.Syncer(s2, sheet2, CFG).flush()
    results += [
        check("restart: Alice 1 sub + 200 bits, once", sheet2.row(2), ["Alice", 1, "", "", 200]),
        check("restart: Bob 1 T3 (once) + 1 T1", sheet2.row(3), ["Bob", 1, "", 1, ""]),
    ]
    s3 = t.Store(tmp)
    results.append(check("restart: nothing pending after clean shutdown", len(s3.pending()), 0))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---- 6. sheet full
full = FakeSheet({f"A{r}": f"user{r}" for r in range(2, 250)})
sf = t.Store(None)
sf.add(ev("overflow", "Overflow", t1=1))
try:
    t.Syncer(sf, full, CFG).flush(); results.append(check("full sheet raises", False, True))
except t.SheetError:
    results.append(check("full sheet: event kept for later", len(sf.pending()), 1))

# ---- 7. chat parsing and gift-bundle de-duplication (lines modelled on real captured chat)
def feed(extractor, lines, fields=("t1", "t2", "t3", "bits")):
    totals = {}
    for line in lines:
        tags, prefix, command, _ = t.parse_irc(line)
        e = extractor.extract(tags, prefix, command)
        if e:
            d = totals.setdefault((e["key"], e["name"]), [0] * len(fields))
            for i, f in enumerate(fields):
                d[i] += e[f]
    return totals

def notice(kind, login, display, plan="1000", extra=""):
    return (f"@display-name={display};id={kind}-{login}-{extra};login={login};msg-id={kind};"
            f"msg-param-sub-plan={plan};room-id=1{';' + extra if extra else ''} :tmi.twitch.tv USERNOTICE #chan")

summary = notice("submysterygift", "gifter", "Gifter", extra="msg-param-mass-gift-count=5;msg-param-community-gift-id=G1")
singles = [notice("subgift", "gifter", "Gifter", extra=f"msg-param-community-gift-id=G1;msg-param-recipient-id={i}") for i in range(5)]
results += [
    check("bundle: summary then 5 gifts = 5", feed(t.EventExtractor(), [summary] + singles), {("gifter", "Gifter"): [5, 0, 0, 0]}),
    check("bundle: gifts first, summary last = 5", feed(t.EventExtractor(), singles + [summary]), {("gifter", "Gifter"): [5, 0, 0, 0]}),
    check("bundle: summary missed = 5", feed(t.EventExtractor(), singles), {("gifter", "Gifter"): [5, 0, 0, 0]}),
    check("bundle: 2 gift lines missed = still 5", feed(t.EventExtractor(), [summary] + singles[:3]), {("gifter", "Gifter"): [5, 0, 0, 0]}),
]
old_summary = notice("submysterygift", "g2", "G2", plan="2000", extra="msg-param-mass-gift-count=3")
old_singles = [notice("subgift", "g2", "G2", plan="2000", extra=f"msg-param-recipient-id={i}") for i in range(3)]
direct = notice("subgift", "g2", "G2", plan="2000", extra="msg-param-recipient-id=99")
results.append(check("bundle without gift id + 1 separate direct gift = 4",
                     feed(t.EventExtractor(), [old_summary] + old_singles + [direct]), {("g2", "G2"): [0, 4, 0, 0]}))

mixed = [
    notice("sub", "primer", "Primer", plan="Prime"),
    notice("resub", "vet", "Vet", plan="3000"),
    notice("resub", "yama", "やま", plan="1000"),
    notice("subgift", "ananonymousgifter", "AnAnonymousGifter", plan="1000", extra="msg-param-recipient-id=5"),
    notice("giftpaidupgrade", "x", "X"),
    notice("viewermilestone", "y", "Y"),
    "@badges=;bits=500;display-name=Deciimator;id=c1;room-id=1 :deciimator!deciimator@deciimator.tmi.twitch.tv PRIVMSG #chan :Cheer500 gg",
    "@badges=;bits=100;display-name=AnAnonymousCheerer;id=c2;room-id=1 :ananonymouscheerer!a@a.tmi.twitch.tv PRIVMSG #chan :Cheer100",
    "@badges=;display-name=Chatter;id=c3;room-id=1 :chatter!c@c.tmi.twitch.tv PRIVMSG #chan :bits=9999 just chatting",
    "@badges=;bits=900;display-name=Other;id=c4;room-id=1;source-room-id=2 :other!o@o.tmi.twitch.tv PRIVMSG #chan :Cheer900",
    "@display-name=Other;id=n9;login=other;msg-id=sub;msg-param-sub-plan=1000;room-id=1;source-room-id=2 :tmi.twitch.tv USERNOTICE #chan",
    "@badges=;bits=50;display-name=Same;id=c5;room-id=1;source-room-id=1 :same!s@s.tmi.twitch.tv PRIVMSG #chan :Cheer50",
]
results.append(check("subs, Prime, anonymous, bits, ignored notices, shared-chat filter", feed(t.EventExtractor(), mixed), {
    ("primer", "Primer"): [1, 0, 0, 0],
    ("vet", "Vet"): [0, 0, 1, 0],
    ("yama", "やま (yama)"): [1, 0, 0, 0],
    (t.ANON_KEY, ""): [1, 0, 0, 100],
    ("deciimator", "Deciimator"): [0, 0, 0, 500],
    ("same", "Same"): [0, 0, 0, 50],
}))
results.append(check("tag unescape", t.parse_irc(r"@a=x\sy\:z\\w :p USERNOTICE #c")[0]["a"], "x y;z\\w"))
results.append(check("channel cleanup", [t.clean_channel(c) for c in ("#Foo", "https://www.twitch.tv/Bar_1/", " baz ")], ["foo", "bar_1", "baz"]))
SID = "1ExampleSheetIdOnlyForTests-abcdefghij_0123"
results.append(check("sheet link or ID -> spreadsheet ID; other links refused", [t.spreadsheet_id_from(s) for s in (
    f"https://docs.google.com/spreadsheets/d/{SID}/edit?usp=sharing",
    f" https://docs.google.com/spreadsheets/d/{SID}/edit#gid=0 ",
    f"docs.google.com/spreadsheets/d/{SID}",
    SID,
    "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/edit",
    "Points",
    "",
)], [SID, SID, SID, SID, "", "", ""]))
results.append(check("spreadsheet ID -> link", (t.sheet_url(SID), t.sheet_url("")),
                     (f"https://docs.google.com/spreadsheets/d/{SID}/edit", "")))

# ---- 8. a switched-off Sheets API is named as the cause, not mistaken for a sharing problem
class FakeAPIError(Exception):
    def __init__(self, error):
        self.error = error

def refused(error):
    try:
        try:
            raise FakeAPIError(error)
        except FakeAPIError as api:
            raise PermissionError from api  # what gspread does with every 403
    except PermissionError as exc:
        return exc

api_off = {"code": 403, "status": "PERMISSION_DENIED", "message": "Google Sheets API has not been used in project 1 before or it is disabled.",
           "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SERVICE_DISABLED",
                        "metadata": {"service": "sheets.googleapis.com"}}]}
not_shared = {"code": 403, "status": "PERMISSION_DENIED", "message": "The caller does not have permission"}
results.append(check("Sheets API switched off is recognised; a sharing problem is not",
                     [t.sheets_api_off(refused(api_off)), t.sheets_api_off(refused(not_shared))], [True, False]))

# ---- 9. Anonymous on the top row (2), people in rows 3-250
TOP = {**CFG, "first_data_row": 3, "anonymous_row": 2}
sheet = FakeSheet({"A2": "Anonymous", "B2": 4, "E2": 300,
                   "A3": "Panpire", "B3": 2, "E3": 50,
                   "F4": 5})                      # nameless Ko-fi row: must not be reused
store = t.Store(None)
store.add(ev(t.ANON_KEY, "", t1=1, bits=100))
store.add(ev("panpire", "Panpire", t1=1))
store.add(ev("newbie", "NewBie", t2=1))
t.Syncer(store, sheet, TOP).flush()
results += [
    check("top layout: Anonymous adds to its own row 2, not to row 3", sheet.row(2), ["Anonymous", 5, "", "", 400]),
    check("top layout: existing person below it is found", sheet.row(3), ["Panpire", 3, "", "", 50]),
    check("top layout: Dollars-only row 4 skipped, new person on row 5", sheet.row(5), ["NewBie", "", 1, "", ""]),
]
empty = FakeSheet()
s = t.Store(None)
s.add(ev("first", "First", t1=1))
s.add(ev(t.ANON_KEY, "", bits=10))
t.Syncer(s, empty, TOP).flush()
results.append(check("top layout, empty sheet: Anonymous on row 2, first person on row 3",
                     [empty.row(2), empty.row(3)], [["Anonymous", "", "", "", 10], ["First", 1, "", "", ""]]))
almost = FakeSheet({f"A{r}": f"user{r}" for r in range(3, 250)})
s = t.Store(None)
s.add(ev("late", "Late", t1=1))
t.Syncer(s, almost, TOP).flush()
results.append(check("top layout: row 250 is usable for people", almost.row(250), ["Late", 1, "", "", ""]))
results.append(check("row layouts", [t.row_layout(CFG), t.row_layout(TOP)[:2], len(t.row_layout(TOP)[2])],
                     [(2, 250, list(range(2, 250))), (2, 250), 248]))

# ---- 10. startup waits out network trouble, but a setup mistake still stops it
attempts = []

class FlakyGoogle:
    def __init__(self, cfg):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("simulated: no internet yet")

def not_shared(cfg):
    raise t.SheetError("simulated: sheet not shared")

class NoWait:
    """Stands in for the stop event: every wait returns at once and Stop is never pressed."""
    def is_set(self):
        return False

    def wait(self, seconds):
        return False

class StopDuringWait:
    """Stop is pressed during the first wait."""
    def __init__(self):
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.stopped = True
        return True

real_sheet = t.GoogleSheet
t.GoogleSheet, t.log.disabled = FlakyGoogle, True
try:
    opened = t.open_sheet({}, NoWait())
    results.append(check("startup: two network failures, then opened", (len(attempts), type(opened).__name__), (3, "FlakyGoogle")))
    attempts.clear()
    results.append(check("startup: Stop while waiting gives up quietly", (t.open_sheet({}, StopDuringWait()), len(attempts)), (None, 1)))
    t.GoogleSheet = not_shared
    try:
        t.open_sheet({}, NoWait())
        raised = False
    except t.SheetError:
        raised = True
    results.append(check("startup: setup mistake is reported instead of retried", raised, True))
finally:
    t.GoogleSheet, t.log.disabled = real_sheet, False

CHEER_FIELDS = ("t1", "t2", "t3", "bits", "small_bits")

# ---- 11. cheers under the minimum go to small_bits (column I) instead of bits; subs are unaffected
def cheer(login, display, bits, msg_id):
    return (f"@badges=;bits={bits};display-name={display};id={msg_id};room-id=1 "
            f":{login}!{login}@{login}.tmi.twitch.tv PRIVMSG #chan :Cheer{bits}")

cheers = [
    cheer("small", "Small", 99, "m1"),
    cheer("exact", "Exact", 100, "m2"),
    cheer("big", "Big", 150, "m3"),
    cheer("ananonymouscheerer", "AnAnonymousCheerer", 50, "m4"),
    cheer("ananonymouscheerer", "AnAnonymousCheerer", 200, "m5"),
    cheer("twice", "Twice", 50, "m6"),
    cheer("twice", "Twice", 50, "m7"),
]
results += [
    check("100-bit minimum: 99 and 50s go to small_bits; 100, 150 and anonymous 200 go to bits in full",
          feed(t.EventExtractor(min_bits=100), cheers, fields=CHEER_FIELDS),
          {("small", "Small"): [0, 0, 0, 0, 99], ("exact", "Exact"): [0, 0, 0, 100, 0], ("big", "Big"): [0, 0, 0, 150, 0],
           (t.ANON_KEY, ""): [0, 0, 0, 200, 50], ("twice", "Twice"): [0, 0, 0, 0, 100]}),
    check("subs are not affected by the cheer minimum",
          feed(t.EventExtractor(min_bits=100), [notice("sub", "primer", "Primer", plan="Prime")], fields=CHEER_FIELDS),
          {("primer", "Primer"): [1, 0, 0, 0, 0]}),
]

# ---- 12. small cheers are added to column I; J (Total cheer) and G/H formulas are never written
def row_with_i(s, r):
    return s.row(r) + [s.cells.get(f"I{r}", "")]

sheet = FakeSheet({"A2": "Anonymous", "B2": 1, "I2": 10,
                   "A3": "shoorei", "E3": 200,
                   "I4": 7})                      # a nameless row with only column I filled: must not be reused
store = t.Store(None)
store.add(ev(t.ANON_KEY, "", small_bits=5))
store.add(ev("shoorei", "shoorei", small_bits=30))
store.add(ev("shoorei", "shoorei", bits=100))
store.add(ev("tiny", "Tiny", small_bits=40))
old = {"id": "", "kind": "cheer", "key": "shoorei", "name": "shoorei", "t1": 0, "t2": 0, "t3": 0, "bits": 300}
store.add(old)                                    # an event saved before column I existed (no small_bits key)
t.Syncer(store, sheet, TOP).flush()
results += [
    check("column I: Anonymous small cheer added to row 2", row_with_i(sheet, 2), ["Anonymous", 1, "", "", "", 15]),
    check("column I: small and full cheers kept apart for one person", row_with_i(sheet, 3), ["shoorei", "", "", "", 600, 30]),
    check("column I: row with only I filled is not reused", row_with_i(sheet, 4), ["", "", "", "", "", 7]),
    check("column I: small-only cheerer gets a row with just Name and I", row_with_i(sheet, 5), ["Tiny", "", "", "", "", 40]),
    check("column I: nothing written outside A-E and I", sorted({k[0] for k in sheet.cells}), ["A", "B", "E", "I"]),
]

# ---- 13. Tracker (used by the window): chat events reach the sheet, Refresh reconnects, Stop ends both threads
fake = FakeSheet({"A2": "Anonymous"})
chat_lines = [cheer("viewer", "Viewer", 40, "g1"), cheer("viewer", "Viewer", 100, "g2")]
connections = []

def fake_chat(channel, on_message, stop, reconnect=None, on_state=None):
    while not stop.is_set():
        connections.append(channel)
        on_state("connected")
        for line in chat_lines:
            on_message(*t.parse_irc(line)[:3])
        chat_lines.clear()
        while not (stop.is_set() or reconnect.is_set()):
            stop.wait(0.05)
        reconnect.clear()
    on_state("stopped")

def wait_for(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.05)

real_chat, real_sheet = t.run_chat, t.GoogleSheet
t.run_chat, t.GoogleSheet, t.log.disabled = fake_chat, (lambda cfg: fake), True
tmp = Path(tempfile.mkdtemp())
try:
    tracker = t.Tracker({**TOP, "channel": "chan", "min_cheer_bits": 100, "flush_seconds": 0}, data_folder=tmp)
    started = tracker.start()
    wait_for(lambda: row_with_i(fake, 3)[4] == 100 and not tracker.pending())
    while_running = row_with_i(fake, 3)
    tracker.refresh()
    wait_for(lambda: len(connections) == 2)
    tracker.stop()
    results += [
        check("Tracker: started", started, True),
        check("Tracker: chat events written while running", while_running, ["Viewer", "", "", "", 100, 40]),
        check("Tracker: Refresh reconnects chat", len(connections), 2),
        check("Tracker: chat stopped on Stop", tracker.chat_state, "stopped"),
        check("Tracker: events also saved on disk", len((tmp / "events.jsonl").read_text(encoding="utf-8").splitlines()), 2),
    ]
finally:
    t.run_chat, t.GoogleSheet, t.log.disabled = real_chat, real_sheet, False
    shutil.rmtree(tmp, ignore_errors=True)

# ---- 14. deleting a person's row: the rows below move up; formulas and the Anonymous row stay put
def full_row(s, r):
    return [s.cells.get(f"{c}{r}", "") for c in "ABCDEFI"]

def refused(fn):
    try:
        fn()
        return "not refused"
    except t.SheetError:
        return "refused"

sheet = FakeSheet({"A2": "Anonymous", "B2": 3, "I2": 9,
                   "A3": "Alpha", "B3": 1, "F3": 5,
                   "A4": "熊猫 (panda_jp)", "D4": 2, "I4": 40,
                   "A5": "Viewer2", "E5": 300, "F5": 12,
                   "A6": "Last", "C6": 1})
sheet.allowed = "ABCDEFI"
sync = t.Syncer(t.Store(None), sheet, TOP)
found = sync.find_row("name", "VIEWER2")
results += [
    check("find by username ignores case", (found["row"], found["name"], found["rows_below"]), (5, "Viewer2", 1)),
    check("find by display name, login or full name", [sync.find_row("name", n)["row"] for n in ("熊猫", "PANDA_JP", "熊猫 (Panda_JP)")], [4, 4, 4]),
    check("find by row number", (sync.find_row("row", " 3 ")["name"], sync.find_row("row", "3")["data"]["F"]), ("Alpha", 5)),
    check("refused: Anonymous row by number and by name, header row, empty row, unknown name",
          [refused(lambda: sync.find_row("row", "2")), refused(lambda: sync.find_row("name", "anonymous")),
           refused(lambda: sync.find_row("row", "1")), refused(lambda: sync.find_row("row", "100")),
           refused(lambda: sync.find_row("name", "nobody"))], ["refused"] * 5),
    check("refused: row changed since it was chosen", (refused(lambda: sync.delete_row(3, "Someone else")), full_row(sheet, 3)),
          ("refused", ["Alpha", 1, "", "", "", 5, ""])),
]
rows = sync.delete_row(4, "熊猫 (panda_jp)")
results += [
    check("delete row 4: rows 4-6 rewritten", rows, [4, 5, 6]),
    check("delete row 4: row 5 moved up, Dollars included", full_row(sheet, 4), ["Viewer2", "", "", "", 300, 12, ""]),
    check("delete row 4: row 6 moved up", full_row(sheet, 5), ["Last", "", 1, "", "", "", ""]),
    check("delete row 4: old last row emptied", full_row(sheet, 6), [""] * 7),
    check("delete row 4: rows above untouched", [full_row(sheet, 2), full_row(sheet, 3)],
          [["Anonymous", 3, "", "", "", "", 9], ["Alpha", 1, "", "", "", 5, ""]]),
    check("delete row 4: formula columns G, H, J never written", sorted({k[0] for k in sheet.cells} - set("ABCDEFI")), []),
]
dup = FakeSheet({"A3": "Dup", "A4": "dup", "B4": 1})
results.append(check("refused: two rows share a username", refused(lambda: t.Syncer(t.Store(None), dup, TOP).find_row("name", "DUP")), "refused"))

bottom = FakeSheet({"A2": "One", "B2": 1, "A3": "Two", "E3": 100, "A250": "Anonymous", "B250": 5})
bottom.allowed = "ABCDEFI"
t.Syncer(t.Store(None), bottom, CFG).delete_row(2, "One")
results.append(check("Anonymous on row 250: delete row 2 moves row 3 up and leaves row 250 alone",
                     [full_row(bottom, 2), full_row(bottom, 3), full_row(bottom, 250)],
                     [["Two", "", "", "", 100, "", ""], [""] * 7, ["Anonymous", 5, "", "", "", "", ""]]))

held = FakeSheet({"A3": "Held", "B3": 1})
held.allowed = "ABCDEFI"
sync = t.Syncer(t.Store(None), held, TOP)
sync._flush_lock.acquire()  # a sheet update is in progress
worker = threading.Thread(target=sync.delete_row, args=(3, "Held"))
worker.start()
time.sleep(0.3)
waited = full_row(held, 3)[0] == "Held"
sync._flush_lock.release()
worker.join(5)
results.append(check("delete waits for a sheet update in progress, then runs", (waited, full_row(held, 3)[0]), (True, "")))

# ---- 15. donations: Dollars (column F) keeps cents, matches names ignoring capitals, survives a lost reply
RATES = {**TOP, "currency_rates": {"USD": 1, "JPY": 0.0065}, "kofi_types": ["Donation", "Subscription"]}
sheet = FakeSheet({"A2": "Anonymous", "F2": 1.5, "A3": "Jo Example", "B3": 1, "F3": 2.25})
sheet.allowed = "ABCDEFI"
store = t.Store(None)
sync = t.Syncer(store, sheet, RATES)
store.add(t.donation_event("kofi", "k1", "JO EXAMPLE", 3.25))
store.add(t.donation_event("streamlabs", "s1", "New Donor", 10))
store.add(t.donation_event("kofi", "k2", "", 4.5))
store.add(t.donation_event("kofi", "k3", "Jo Example", 0.1))
sync.flush()
results += [
    check("donations: added to an existing row by name (ignoring capitals), cents kept",
          full_row(sheet, 3), ["Jo Example", 1, "", "", "", 5.6, ""]),
    check("donations: a new donor gets a row with only Name and Dollars", full_row(sheet, 4), ["New Donor", "", "", "", "", 10, ""]),
    check("donations: anonymous goes to the Anonymous row", full_row(sheet, 2)[5], 6.0),
    check("donations: the same donation ID is never saved twice", store.add(t.donation_event("kofi", "k1", "Jo Example", 3.25)), False),
]
sheet.fail = "after"
store.add(t.donation_event("kofi", "k4", "Jo Example", 1.15))
try:
    sync.flush()
except TimeoutError:
    pass
sync.flush()
results.append(check("donations: a lost reply does not count cents twice", full_row(sheet, 3)[5], 6.75))

# ---- 16. Ko-fi tab rows and Streamlabs socket messages become events; the rest are explained, not counted
def kofi_result(row):
    event, status = t.kofi_event(row, RATES)
    return (event["key"], event["dollars"], status) if event else (None, None, status)

results.append(check("Ko-fi rows: donation, subscription, private, yen converted; others refused with a reason", [kofi_result(r) for r in (
    ["", "", "Donation", "Jo", 3, "USD", "hi", "yes", "tx1", "m1"],
    ["", "", "Subscription", "Sub Person", "5.00", "usd", "", "yes", "tx2", "m2"],
    ["", "", "Donation", "Secret", 2, "USD", "", "no", "tx3", "m3"],
    ["", "", "Donation", "Yen Fan", 1000, "JPY", "", "yes", "tx4", "m4"],
    ["", "", "Shop Order", "Buyer", 20, "USD", "", "yes", "tx5", "m5"],
    ["", "", "Donation", "Euro Fan", 5, "EUR", "", "yes", "tx6", "m6"],
    ["", "", "Donation", "No Id", 5, "USD", "", "yes", "", ""],
)], [
    ("jo", 3.0, "added"), ("sub person", 5.0, "added"), (t.ANON_KEY, 2.0, "added"), ("yen fan", 6.5, "added"),
    (None, None, "not counted: Shop Order"), (None, None, "not counted: no rate for EUR in currency_rates"),
    (None, None, "not counted: no message ID"),
]))

class FakeInbox:
    def __init__(self, rows):
        self.data = {n: list(r) for n, r in rows.items()}

    def rows(self):
        return sorted(self.data.items())

    def mark(self, statuses):
        for row, text in statuses.items():
            self.data[row] = (self.data[row] + [""] * 11)[:10] + [text]

inbox = FakeInbox({
    2: ["", "", "Donation", "Jo", 3, "USD", "", "yes", "tx1", "m1"],
    3: ["", "", "Shop Order", "Buyer", 20, "USD", "", "yes", "tx5", "m5"],
    4: ["", "", "Donation", "Script_Test", 1, "USD", "", "yes", "test", "test-1", "skip"],
})
store = t.Store(None)
t.log.disabled = True
try:
    first = t.check_kofi(inbox, store, RATES)
    again = t.check_kofi(inbox, store, RATES)
    inbox.data[2][10] = ""  # as if writing "added" had failed: it must still not count twice
    third = t.check_kofi(inbox, store, RATES)
finally:
    t.log.disabled = False
results.append(check("Ko-fi tab: new rows counted once, Tracker column filled, rows marked skip left alone",
                     (first, again, third, len(store.pending()), [inbox.data[r][10] for r in (2, 3, 4)]),
                     (1, 0, 0, 1, ["added", "not counted: Shop Order", "skip"])))

payload = {"type": "donation", "message": [
    {"id": 101, "name": "Tipper", "amount": "13.37", "currency": "USD", "formatted_amount": "$13.37"},
    {"id": 102, "name": "Anonymous", "amount": 5, "currency": "USD"},
    {"id": 103, "name": "Yen", "amount": "500", "currency": "JPY"},
    {"id": 104, "name": "Euro", "amount": "5", "currency": "EUR"},
    {"id": 105, "name": "Tester", "amount": "1", "currency": "USD", "isTest": True},
    {"name": "No Id", "amount": "1", "currency": "USD"},
]}
results += [
    check("Streamlabs donations: counted, anonymous, converted; unknown currency, tests and missing IDs refused",
          [(e["id"], e["key"], e["dollars"]) if e else (None, why.split(" (")[0])
           for e, why in t.streamlabs_events(payload, RATES)],
          [("streamlabs:101", "tipper", 13.37), ("streamlabs:102", t.ANON_KEY, 5.0), ("streamlabs:103", "yen", 3.25),
           (None, "no rate for EUR in currency_rates"), (None, "test alert"), (None, "no donation ID")]),
    check("Streamlabs follows, subs and bits are left to chat", t.streamlabs_events({"type": "follow", "message": [{"name": "x"}]}, RATES), []),
]

# ---- 17. the Streamlabs socket: Socket.IO 2 greeting, pings, events, a refused token; the token never reaches the log
class FakeSocket:
    def __init__(self, frames, stop):
        self.frames, self.stop, self.sent = list(frames), stop, []

    def recv(self):
        if not self.frames:
            raise t.websocket.WebSocketTimeoutException()
        frame = self.frames.pop(0)
        if not self.frames:
            self.stop.set()  # end the test after this frame
        return frame

    def send(self, text):
        self.sent.append(text)

    def settimeout(self, seconds):
        pass

    def close(self):
        pass

class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())

def socket_run(frames, token):
    stop, sockets, urls, events, states = threading.Event(), [], [], [], []

    def connect(url, timeout):
        urls.append(url)
        sockets.append(FakeSocket(frames, stop))
        return sockets[-1]

    capture, real_connect = Capture(), t.websocket.create_connection
    t.websocket.create_connection = connect
    t.log.addHandler(capture)
    t.log.propagate = False
    try:
        t.run_streamlabs(token, events.append, stop, None, states.append)
    finally:
        t.websocket.create_connection = real_connect
        t.log.removeHandler(capture)
        t.log.propagate = True
    return urls, sockets, events, states, capture.lines

greeting = '0{"sid":"x","upgrades":[],"pingInterval":0,"pingTimeout":20000}'
urls, sockets, events, states, lines = socket_run(
    [greeting, "40", '42["event",{"type":"donation","message":[{"id":7,"name":"A","amount":"2","currency":"USD"}]}]'],
    "se cret/token")
results.append(check("Streamlabs socket: token sent safely, pings sent, event delivered, states reported", (
    urls[0].endswith("token=se%20cret%2Ftoken"), "2" in sockets[0].sent, events[0]["message"][0]["id"], states),
    (True, True, 7, ["connecting", "connected", "stopped"])))
urls, sockets, events, states, lines = socket_run([greeting, '44"Authentication error"'], "bad-token-123")
results.append(check("Streamlabs socket: a refused token is reported, without the token itself", (
    events, states, any("refused the socket token" in l for l in lines), any("bad-token-123" in l for l in lines)),
    ([], ["connecting", "refused", "stopped"], True, False)))

# ---- 18. Tracker with donations switched on: Streamlabs and Ko-fi donations reach the sheet next to chat
fake = FakeSheet({"A2": "Anonymous"})
fake.allowed = "ABCDEFI"
kofi_inbox = FakeInbox({2: ["", "", "Donation", "Kofi Fan", 4, "USD", "", "yes", "tx9", "m9"]})

def fake_streamlabs(token, on_event, stop, reconnect=None, on_state=None):
    on_state("connected")
    on_event({"type": "donation", "message": [{"id": 55, "name": "Labs Fan", "amount": "2.50", "currency": "USD"}]})
    stop.wait()
    on_state("stopped")

def quiet_chat(channel, on_message, stop, reconnect=None, on_state=None):
    on_state("connected")
    stop.wait()
    on_state("stopped")

real = (t.run_chat, t.run_streamlabs, t.GoogleSheet, t.KofiInbox)
t.run_chat, t.run_streamlabs, t.GoogleSheet = quiet_chat, fake_streamlabs, (lambda cfg: fake)
t.KofiInbox = lambda sheet, tab: kofi_inbox
t.log.disabled = True
tmp = Path(tempfile.mkdtemp())
try:
    tracker = t.Tracker({**RATES, "channel": "chan", "min_cheer_bits": 100, "flush_seconds": 0,
                         "streamlabs_socket_token": "tok", "kofi_tab": "Ko-fi", "kofi_check_seconds": 5}, data_folder=tmp)
    started = tracker.start()
    wait_for(lambda: full_row(fake, 3)[0] and full_row(fake, 4)[0] and not tracker.pending())
    states_running = (tracker.streamlabs_state, tracker.kofi_state)
    tracker.stop()
    rows = sorted([full_row(fake, 3), full_row(fake, 4)])
    results += [
        check("Tracker: both donation sources started", (started, states_running), (True, ("connected", "ok"))),
        check("Tracker: Streamlabs and Ko-fi donations written to Dollars",
              rows, [["Kofi Fan", "", "", "", "", 4, ""], ["Labs Fan", "", "", "", "", 2.5, ""]]),
        check("Tracker: Ko-fi row marked added", kofi_inbox.data[2][10], "added"),
    ]
finally:
    t.run_chat, t.run_streamlabs, t.GoogleSheet, t.KofiInbox = real
    t.log.disabled = False
    shutil.rmtree(tmp, ignore_errors=True)

# ---- 19. donations stay off unless switched on in config.json
plain = t.Tracker({**TOP, "channel": "chan", "min_cheer_bits": 100, "flush_seconds": 3, **{
    k: t.DEFAULT_CONFIG[k] for k in ("streamlabs_socket_token", "kofi_tab", "kofi_types", "kofi_check_seconds", "currency_rates")}})
results.append(check("donations are off by default", (plain.streamlabs_state, plain.kofi_state,
                     t.DEFAULT_CONFIG["streamlabs_socket_token"], t.DEFAULT_CONFIG["kofi_tab"]), ("off", "off", "", "")))

# ---- 20. the Ko-fi script and the tracker agree on the Ko-fi tab's columns
import re
script = (Path(sys.argv[1]).resolve().parent / "kofi_webhook.gs").read_text(encoding="utf-8")
script_headers = re.findall(r"'([^']*)'", re.search(r"const HEADERS = \[(.*?)\];", script, re.S).group(1))
results += [
    check("Ko-fi tab: the script's first columns are exactly the ones the tracker reads",
          (script_headers[:len(t.KOFI_HEADERS)], script_headers[len(t.KOFI_HEADERS):]), (t.KOFI_HEADERS, ["Forwarded"])),
    check("Ko-fi tab: a row with a Forwarded value is still read normally",
          kofi_result(["", "", "Donation", "Jo", 3, "USD", "", "yes", "tx1", "m1", "", "sent"]), ("jo", 3.0, "added")),
    check("Ko-fi script on GitHub keeps placeholders, not a token or forwarding address",
          (re.search(r"const VERIFICATION_TOKEN = '([^']*)'", script).group(1), re.search(r"const FORWARD_TO = '([^']*)'", script).group(1)),
          ("PASTE_THE_KOFI_VERIFICATION_TOKEN_HERE", "")),
]

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)

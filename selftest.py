"""Offline tests for the tracker: fake sheet, injected failures, gift-bundle de-duplication."""
import importlib.util, shutil, sys, tempfile, threading, time
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
          feed(t.EventExtractor(min_bits=100), cheers, fields=t.FIELDS),
          {("small", "Small"): [0, 0, 0, 0, 99], ("exact", "Exact"): [0, 0, 0, 100, 0], ("big", "Big"): [0, 0, 0, 150, 0],
           (t.ANON_KEY, ""): [0, 0, 0, 200, 50], ("twice", "Twice"): [0, 0, 0, 0, 100]}),
    check("subs are not affected by the cheer minimum",
          feed(t.EventExtractor(min_bits=100), [notice("sub", "primer", "Primer", plan="Prime")], fields=t.FIELDS),
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

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)

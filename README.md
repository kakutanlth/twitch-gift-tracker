# Twitch Gift Tracker

A small Windows app that listens to a Twitch channel's chat and adds every sub, gifted sub and bits cheer to a Google Sheet as it happens. It was built for the Pandathon event sheet, so the window is called **Pandathon Tracker**.

- Counts new subs, resubs, Prime subs and gifted subs (credited to the gifter), by tier.
- Counts cheers: 100 bits or more in one message go to **Bits**; smaller cheers go to their own column.
- Puts anonymous gifts and cheers on one **Anonymous** row.
- Optionally adds **Ko-fi** and **Streamlabs** donations to the donor's **Dollars**, as they arrive.
- Reads chat anonymously: no Twitch login or Twitch app is needed.
- Saves every event on your PC before writing it to the sheet, so restarts and Google outages do not lose or double-count anything.
- Comes with a window (Start/Stop, Refresh, Sheet config, Delete row, light/dark theme) and a plain console version.

---

## Contents

1. [What you need](#what-you-need)
2. [Installation](#installation)
   1. [Install Python 3.14](#1-install-python-314)
   2. [Download the tracker](#2-download-the-tracker)
   3. [Create the Google key](#3-create-the-google-key)
   4. [Prepare the Google Sheet](#4-prepare-the-google-sheet)
   5. [First start](#5-first-start)
   6. [Test it before the event](#6-test-it-before-the-event)
3. [Using the tracker](#using-the-tracker)
4. [What gets counted](#what-gets-counted)
5. [Ko-fi and Streamlabs donations (optional)](#ko-fi-and-streamlabs-donations-optional)
6. [Configuration reference](#configuration-reference)
7. [Files](#files)
8. [Troubleshooting](#troubleshooting)
9. [Keeping your keys safe](#keeping-your-keys-safe)

---

## What you need

- A Windows 10 or 11 PC that stays on (and awake) during the event.
- Python 3.14 (step 1 below).
- A Google account, for a free Google Cloud project and the Google Sheet.
- The Twitch channel name of the streamer.

---

## Installation

### 1. Install Python 3.14

Pick **one** of the two options.

**Option A: with winget (recommended).** Open *Windows Terminal* or *PowerShell* and run:

```powershell
winget install --id Python.Python.3.14 --exact --source winget --scope user --custom "PrependPath=1"
```

This installs Python 3.14 for your Windows account only (no administrator rights needed) and adds it to `PATH`.

**Option B: from python.org.**

1. Go to <https://www.python.org/downloads/windows/> and download the **Windows installer (64-bit)** for the latest Python 3.14 release.
2. Run it. On the first screen, tick **Add python.exe to PATH**.
3. Leave **tcl/tk and IDLE** ticked if you choose *Customize installation* (it is ticked by default). The tracker window needs it.
4. Finish the installation.

**Check it worked.** Close the terminal, open a **new** one, and run:

```powershell
python --version
python -c "import tkinter; print('tkinter OK')"
```

You should see `Python 3.14.x` and `tkinter OK`.

> If you see *"Python was not found; run without arguments to install from the Microsoft Store"*, Windows is still using its Store shortcut. Make sure you opened a new terminal after installing. If it still happens, go to **Settings > Apps > Advanced app settings > App execution aliases** and turn off the two *App Installer python* entries.

### 2. Download the tracker

On the repository page, choose **Code > Download ZIP** and extract it to a folder you can write to, for example `D:\Tracker`. Avoid `C:\Program Files`, which needs administrator rights.

Or, with Git:

```powershell
git clone https://github.com/kakutanlth/twitch-gift-tracker.git D:\Tracker
```

### 3. Create the Google key

The tracker writes to Google Sheets with a *service account*: a robot Google account that only has access to the sheets you share with it. You do this once.

1. Go to <https://console.cloud.google.com/> and create a project (any name, for example *Twitch Tracker*).
2. In **APIs & Services > Library**, search for **Google Sheets API** and press **Enable**. Make sure the project picker at the top shows the project you just created.
3. In **APIs & Services > Credentials**, choose **Create credentials > Service account**. Give it a name and press **Done**; it needs no roles.
4. Open the new service account, go to **Keys > Add key > Create new key**, choose **JSON**, and press **Create**. A `.json` file downloads.
5. Rename that file to **`service_account.json`** and move it into the tracker folder, next to `pandathon_tracker.py`.

Note the service account's email address (it ends in `.iam.gserviceaccount.com`). You will share the sheet with it in the next step; the tracker window also shows it under **Sheet config**.

> `service_account.json` is a password. Do not share it, email it, or commit it to Git. See [Keeping your keys safe](#keeping-your-keys-safe).

### 4. Prepare the Google Sheet

The tracker expects one tab (sheet) laid out like this. The default tab name is `Points`; you can use any name and set it in **Sheet config**.

| Column | Row 1 heading | Who fills it |
|---|---|---|
| A | Name | Tracker |
| B | Tier 1 Subs | Tracker (Prime subs count as Tier 1) |
| C | Tier 2 Subs | Tracker |
| D | Tier 3 Subs | Tracker |
| E | Bits | Tracker (cheers of 100 bits or more) |
| F | Dollars | You, or the tracker once [Ko-fi or Streamlabs donations](#ko-fi-and-streamlabs-donations-optional) are switched on |
| G | Bags | Your formula |
| H | Total | Your formula |
| I | <100 Bits cheers | Tracker (cheers under 100 bits) |
| J | Total cheer | Your formula |

- **Row 2** is the **Anonymous** row. The tracker writes "Anonymous" in A2 itself.
- **Rows 3 to 250** are for people. Each new person takes the first empty row.
- The sheet needs **at least 250 rows**.
- The tracker never writes to columns G, H and J, so your formulas are safe. As an example, the Pandathon sheet uses:
  - `G2`: `=B2*(3)+C2*(5)+D2*(12)+E2*(0.01)+F2`, filled down to row 250
  - `H2`: `=SUM(G2:G250)`
  - `J2`: `=SUM(E2,I2)`, filled down to row 250

Finally, press **Share** in Google Sheets and share the spreadsheet with the service account's email address as **Editor**.

The row numbers can be changed (for example, Anonymous on the last row instead); see [Configuration reference](#configuration-reference).

### 5. First start

Double-click **`run_tracker_gui.bat`** in the tracker folder.

The first time, it:

- creates a private Python environment (`.venv`) and installs the two libraries from `requirements.txt`. This needs internet and takes a minute.
- creates `config.json` from `config.example.json`.

Then the tracker window opens. Fill in the two settings:

1. Under **Settings**, type the **Twitch channel** (the name in `twitch.tv/<name>`) and press **Save settings**. Leave **Cheer minimum (bits)** at 100 unless your event uses a different cut-off.
2. Press **Sheet config**. Paste the sheet's link from your browser's address bar into **Sheet URL**, type the tab name into **Sheet name**, and press **Save**. The tracker checks the sheet with Google and shows the spreadsheet's name and row 1. Press **Yes** to save.

### 6. Test it before the event

Do these once, before the stream:

1. **Self-test (offline):** in a terminal in the tracker folder, run

   ```powershell
   .venv\Scripts\python.exe selftest.py pandathon_tracker.py
   ```

   It should end with `80/80 passed`.

2. **Watch chat without writing anything:**

   ```powershell
   .\run_tracker.bat --dry-run
   ```

   It prints `Connected. Listening to #<channel> ...` and every sub or cheer it sees. Nothing is saved or written to the sheet. Press **Ctrl+C** to stop.

3. **Write a test row to the sheet:**

   ```powershell
   .\run_tracker.bat --test-sheet
   ```

   This adds a row named `Tracker_Test` with 1 Tier 1 sub, 100 bits and 50 in column I. Remove it afterwards with **Delete row ... > Delete username > `tracker_test`** in the window.

---

## Using the tracker

Start the window with **`run_tracker_gui.bat`**. The buttons and settings are in the top half, and the live log is in the bottom half.

| Control | What it does |
|---|---|
| **Start tracking / Stop tracking** | One button. Start saves the settings, opens the sheet and connects to chat. If Google cannot be reached, it keeps trying (after 5 seconds, then up to every 60 seconds). Stop writes anything still waiting, then disconnects. |
| **Refresh** | Reconnects to chat (and Streamlabs), checks the Ko-fi tab, and writes any saved events to the sheet right away. Only while tracking. |
| **Sheet config** | Popup with the **Sheet URL** and **Sheet name**, then **Save**, **Cancel** and **Open sheet**. Save checks the sheet with Google and asks you to confirm, or explains what is wrong (a wrong sheet name lists the real tab names). The sheet can only be changed while tracking is stopped. **Open sheet** opens the saved sheet in your browser. |
| **Delete row ...** | Popup to remove one person: choose **Delete row number** or **Delete username** (not case sensitive), type it, and press **Delete**. A warning shows that row's numbers before anything changes. See [Deleting a row](#deleting-a-row). |
| **☾ Dark / ☀ Light** (top right) | Switches the window between dark and light. The choice is remembered; the first time, it follows your Windows setting. |
| **Settings** | **Twitch channel** and **Cheer minimum (bits)**, saved to `config.json`. They can be changed while tracking is stopped. |

The line under the title shows the state (**Stopped**, **Starting**, **Tracking #channel**, **Stopping**), whether chat (and Streamlabs and the Ko-fi tab, when switched on) is connected, and how many events are waiting to be written to the sheet. Warnings in the log are amber and errors are red; the log is also saved to `data/tracker.log`.

**During the event:**

- Press **Start tracking** and leave the window open.
- Turn off sleep on the PC. A sleeping PC is disconnected from chat, and Twitch chat has no replay: anything that happens while the tracker is stopped, asleep or offline is missed.
- Closing the window while tracking asks first, then stops cleanly.

### Deleting a row

**Delete row ...** empties the chosen person's data (columns A to F and I) and moves every person below it up by one row. The formula columns G, H and J are not touched, so your totals keep working, and the sheet keeps all its rows.

It refuses to delete the header row, the Anonymous row, an empty row, or a username that matches more than one row (delete one of those by row number instead). If the row changed between the warning and your **Yes**, nothing is deleted.

While tracking, a delete waits for any sheet update in progress, so the two never mix. The tracker cannot undo a delete, but Google Sheets can: **File > Version history**.

### Console version

`run_tracker.bat` runs the same tracker in a plain console window, without the buttons:

| Command | What it does |
|---|---|
| `run_tracker.bat` | Track for real. Press Ctrl+C to stop. |
| `run_tracker.bat --dry-run` | Only print what chat shows; save and write nothing. |
| `run_tracker.bat --test-sheet` | Write one test row to the sheet and exit. |
| `run_tracker.bat --channel NAME` | Use a different channel than the one in `config.json`, for this run only. |

Do not run the window and the console version at the same time; both would write to the same sheet.

---

## What gets counted

| Chat event | Where it goes |
|---|---|
| New sub or resub | +1 in Tier 1, 2 or 3 for that viewer |
| Prime sub | +1 in Tier 1 |
| Multi-month sub | +1 (it counts as one sub) |
| Gifted sub(s) | +1 per gift for the **gifter**. A bundle of 5 gifts counts as 5, once |
| Cheer of 100 bits or more in one message | The full amount in **Bits** (column E) |
| Cheer under 100 bits | The amount in **<100 Bits cheers** (column I) |
| Anonymous gift or anonymous cheer | The Anonymous row, by the same rules |
| Ko-fi or Streamlabs donation (when switched on) | The amount in **Dollars** (column F) for the donor's name; private or anonymous donations on the Anonymous row |

Not counted:

- A viewer continuing a gifted sub, or converting a Prime sub to a paid one. These are not new payments.
- "Pay it forward" notices. The gift itself is counted.
- Resubs the viewer does not share in chat. Twitch only announces shared resubs.
- Bits spent on Power-ups.
- Events from the other channel during a Shared Chat session.

**Names:** a viewer's row is found by name, ignoring capitals. If the display name is not just the login (for example, a non-Latin name), the login is added in brackets, like `熊猫 (panda_jp)`, and the row is matched by that login.

---

## Ko-fi and Streamlabs donations (optional)

The tracker can add Ko-fi and Streamlabs donations to **Dollars (column F)** of the donor's row, next to their subs and bits. Both are **off** until you switch them on in `config.json`. Everything else works the same either way.

Before you switch them on:

- **Stop entering these donations into Dollars by hand**, or they will be counted twice.
- **Donors type their own name** on Ko-fi and Streamlabs. The tracker matches it to the Name column, ignoring capitals, like a Twitch name. If a donor uses a different name, they get a separate row; move the amount by hand and remove the extra row with **Delete row**.
- **Set the currency.** `currency_rates` in `config.json` turns each donation into the sheet's money. The default, `{"USD": 1}`, counts US dollars as they are. To also accept, say, yen at a fixed rate you choose: `{"USD": 1, "JPY": 0.0065}`. A donation in a currency that is not listed is **not counted**, and the log says so.

### Streamlabs

The tracker listens to the streamer's Streamlabs **Socket API**, which announces each Streamlabs tip with its exact amount and an ID.

1. **The streamer:** log in at [streamlabs.com](https://streamlabs.com), open **Settings** (under *Account* in the left sidebar) > **API Settings** > **API Tokens**, and press **Copy** next to **Your Socket API Token**. Send it to you privately, never in chat. It can be reset on the same page if it ever leaks.
2. **You:** add it to `config.json`:

   ```json
   "streamlabs_socket_token": "the token",
   ```

3. Start tracking. The status line shows **Streamlabs connected**, and each tip appears in the log as `DONATE     5.00  Name (Streamlabs)`.

Notes:

- Tips are only received **while tracking is running**; Streamlabs does not send them again later.
- Only Streamlabs tips are counted from it. Follows, subs and bits that Streamlabs also announces are ignored, because the tracker counts those from chat. Ko-fi donations do not come through Streamlabs.
- Alerts marked as tests are ignored. If a test is counted anyway, remove it with **Delete row** (or fix the amount by hand).

### Ko-fi

Ko-fi sends each donation to a web address (a *webhook*). `kofi_webhook.gs` is a small Google Apps Script, attached to your spreadsheet, that receives them and writes each one as a row on a **Ko-fi** tab. While tracking, the tracker checks that tab every 20 seconds and adds new rows to Dollars. Donations made while the tracker is off wait on the tab until it runs again, and each one is counted only once.

1. **The streamer:** log in at [ko-fi.com](https://ko-fi.com) and open **Settings > More > API** (or go to `https://ko-fi.com/manage/webhooks`).
   - Check whether the **webhook URL** box already has an address. Ko-fi allows only one; replacing it would break whatever uses it now (a bot or alert tool). If it is in use, stop and sort that out first.
   - Open **Advanced**, copy the **Verification Token**, and send it to you privately.
2. **You:** open the Google Sheet, then **Extensions > Apps Script**. Delete everything in the editor, paste the whole of `kofi_webhook.gs`, put the token between the quotes on the `VERIFICATION_TOKEN` line, and press **Save**.
3. **Test the script:** choose `testDonation` in the function list at the top and press **Run**. Google asks for permission the first time; your own script is unverified, so choose **Advanced > Go to (your project) (unsafe)** and **Allow**. A **Ko-fi** tab appears with a `Script_Test` row. Type `skip` in that row's **Tracker** column.
4. **Publish it:** **Deploy > New deployment**, press the gear next to *Select type* and choose **Web app**. Set **Execute as: Me** and **Who has access: Anyone**, press **Deploy**, and copy the **Web app URL** (it ends in `/exec`).
5. **The streamer:** paste that URL into Ko-fi's **webhook URL** box and press **Update**. Then send a test from the same page: a new row should appear on the Ko-fi tab within seconds. Type `skip` in its Tracker column too.
6. **You:** add this to `config.json`, then start tracking:

   ```json
   "kofi_tab": "Ko-fi",
   ```

   The status line shows **Ko-fi tab checked**. Each row the tracker counts gets `added` in its **Tracker** column; other rows get `not counted:` and the reason.

How the Ko-fi tab works:

- Columns: Received, Ko-fi time, Type, Name, Amount, Currency, Message, Public, Transaction ID, Message ID, Tracker.
- Only the types in `kofi_types` are counted (`Donation` and `Subscription` by default). Shop orders and commissions are listed but not counted.
- A **private** donation (the supporter chose not to show it publicly) keeps its name and message off the sheet, and its amount goes to the Anonymous row.
- Supporters' email addresses are never stored.
- To leave a row out, type `skip` in its Tracker column. Do not delete rows from the Ko-fi tab while tracking.
- If you change the script later, publish it with **Deploy > Manage deployments**, the pencil icon, **Version: New version**, **Deploy**. That keeps the same web address; a *new* deployment would get a new one, and the streamer would have to paste it into Ko-fi again.

---

## Configuration reference

`config.json` is created from `config.example.json` on first start. In the window, **Settings** changes `channel` and `min_cheer_bits`, and **Sheet config** changes `spreadsheet_id` and `worksheet`. The other keys can be edited in any text editor while the tracker is stopped.

| Key | Default | Meaning |
|---|---|---|
| `channel` | (empty) | Twitch channel to listen to. `Name`, `#name` and `https://www.twitch.tv/name` all work. |
| `spreadsheet_id` | (empty) | The long code between `/d/` and `/edit` in the sheet's link. Sheet config fills it from the link. |
| `worksheet` | `Points` | Name of the tab to write to. |
| `credentials_file` | `service_account.json` | The Google key file, relative to the tracker folder. |
| `first_data_row` | `3` | First row for people. |
| `last_data_row` | `250` | Last row for people. |
| `anonymous_row` | `2` | The Anonymous row. It can be above, below or among the people rows. |
| `anonymous_name` | `Anonymous` | Name written in column A of the Anonymous row. |
| `min_cheer_bits` | `100` | Cheers below this (in one message) go to column I instead of Bits. |
| `flush_seconds` | `3` | How long to collect a burst of events (such as a gift bundle) into one sheet update. |
| `streamlabs_socket_token` | (empty) | The streamer's Streamlabs Socket API token. Empty: Streamlabs donations are off. |
| `kofi_tab` | (empty) | The tab `kofi_webhook.gs` writes to, normally `Ko-fi`. Empty: Ko-fi donations are off. |
| `kofi_types` | `["Donation", "Subscription"]` | Which Ko-fi payment types count. |
| `kofi_check_seconds` | `20` | How often the Ko-fi tab is checked while tracking. |
| `currency_rates` | `{"USD": 1}` | How much one unit of each currency adds to Dollars. Donations in other currencies are not counted. |

Example: Anonymous on the **last** row instead, with people in rows 2 to 249:

```json
"first_data_row": 2,
"last_data_row": 250,
"anonymous_row": 250,
```

---

## Files

| File | Purpose |
|---|---|
| `run_tracker_gui.bat` | Double-click to open the tracker window. Sets up `.venv` on first run. |
| `run_tracker.bat` | The console version. |
| `pandathon_gui.py` | The window. |
| `pandathon_tracker.py` | The tracker itself: chat, counting, the event log and the sheet. |
| `selftest.py` | Offline tests: `.venv\Scripts\python.exe selftest.py pandathon_tracker.py` |
| `requirements.txt` | The two Python libraries: `websocket-client` and `gspread`. |
| `config.example.json` | Template for `config.json`. |
| `kofi_webhook.gs` | The Google Apps Script that receives Ko-fi donations (optional). Paste it into the spreadsheet; this copy keeps a placeholder for the token. |
| `config.json` | Your settings. Created on first run; **not** in Git. |
| `service_account.json` | Your Google key. You add it; **not** in Git. |
| `data/events.jsonl` | Every counted event, saved before it is written to the sheet. |
| `data/state.json` | How far the sheet has been brought up to date. |
| `data/tracker.log` | The log. |
| `data/window.json` | The window's theme choice. |

Events that were saved but not yet written (for example, during an internet outage) are written automatically the next time tracking runs.

---

## Troubleshooting

| Message or problem | What to do |
|---|---|
| *Python was not found; run without arguments to install from the Microsoft Store* | Python is not installed or not on `PATH`. Repeat [step 1](#1-install-python-314) and open a new terminal. |
| *This Python has no Tkinter, which the window needs.* | Run the Python installer again, choose **Modify**, and tick **tcl/tk and IDLE**. |
| *Setup failed. Is Python 3 installed and on PATH?* | Same as the first row. Also check the internet connection: the first run downloads two libraries. |
| *Google key file not found* | Put `service_account.json` in the tracker folder ([step 3](#3-create-the-google-key)). |
| *The Google Sheets API is switched off in the Google Cloud project ...* | Open the link in the message, press **Enable**, wait a few minutes, and try again. |
| *Cannot open the spreadsheet. Share it with ... as Editor* | Share the sheet with the service account's email as Editor, and check the link under Sheet config. |
| *The spreadsheet has no tab called 'X'. Its tabs are: ...* | Fix the **Sheet name** under Sheet config. |
| *The sheet only has N rows, but the tracker uses rows down to 250.* | Add rows to the sheet (and fill your formulas down), or lower `last_data_row`. |
| *Twitch has not confirmed joining #channel. Is the channel name right?* | Check the channel name under Settings. |
| *No empty row left for new people in rows 3 to 250.* | Free up rows, or raise `last_data_row` (and add rows and formulas). Nothing is lost: the tracker catches up by itself. |
| *Could not update the sheet (...). Events are saved; retrying in N s.* | Usually a short Google or internet outage. It retries by itself; press **Refresh** to retry now. |
| *Streamlabs refused the socket token* | The token is wrong or was reset. Ask the streamer for the current one. |
| *Ko-fi donations are off: The spreadsheet has no tab called 'Ko-fi'* | The script has not written its first row yet, or `kofi_tab` does not match the tab's name. Run `testDonation` in the script once. |
| *Ko-fi tab row N not counted: no rate for JPY in currency_rates* (or *Streamlabs donation not counted: ...*) | Add that currency to `currency_rates`, then type the amount into Dollars by hand for that donation. |
| A Ko-fi donation never appears on the Ko-fi tab | Check the Web app URL in Ko-fi's webhook box, that **Who has access** is **Anyone**, and that `VERIFICATION_TOKEN` in the script matches Ko-fi's. |
| The window does not appear | In a terminal in the tracker folder, run `.venv\Scripts\python.exe pandathon_gui.py` to see the error. |

---

## Keeping your keys safe

- `service_account.json` can write to every sheet shared with it. The `.gitignore` in this repository keeps it, `config.json` and the `data/` folder out of Git. Check `git status` before every commit.
- The Streamlabs socket token lives in `config.json`, so Git ignores it too. The Ko-fi verification token belongs only in the copy of `kofi_webhook.gs` inside the spreadsheet; keep the copy in this repository with its placeholder.
- If the key was ever shared or committed, replace it: in Google Cloud, open the service account, go to **Keys**, delete the old key, and create a new one ([step 3](#3-create-the-google-key), parts 4 and 5). Deleting a file from Git does not remove it from the repository's history.

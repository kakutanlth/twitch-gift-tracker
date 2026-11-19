/**
 * Ko-fi -> Google Sheet, for the Twitch Gift Tracker.
 *
 * Ko-fi sends every donation to this script's web address (a "webhook"). The script writes each one as a row
 * on a "Ko-fi" tab in this spreadsheet; the tracker reads that tab and adds the amounts to Dollars (column F).
 * Donations made while the tracker is off wait on the tab until it runs again.
 *
 * Ko-fi allows only one webhook address. If the streamer's Ko-fi already sends donations somewhere (for example
 * a Megathon timer widget), put that address in FORWARD_TO: every donation is passed on there first, exactly as
 * Ko-fi sent it, so it keeps working.
 *
 * Setup (details in README.md, "Ko-fi and Streamlabs donations"):
 *   1. In the spreadsheet: Extensions > Apps Script. Replace everything in the editor with this file.
 *   2. Paste the Ko-fi Verification Token below (Ko-fi: Settings > More > API > Advanced), and the old webhook
 *      address into FORWARD_TO if there was one. Save.
 *   3. Deploy > New deployment > Web app. Execute as: Me. Who has access: Anyone. Deploy, then allow access.
 *   4. Give the Web app URL (it ends in /exec) to the streamer to paste into Ko-fi's webhook box.
 *
 * The token and the forwarding address belong only in the copy inside the spreadsheet; keep the copy on GitHub
 * as it is.
 */

const VERIFICATION_TOKEN = 'PASTE_THE_KOFI_VERIFICATION_TOKEN_HERE';

/** The address Ko-fi used to send donations to before this script, if any (for example a widget's webhook address). */
const FORWARD_TO = '';

const TAB_NAME = 'Ko-fi';
const HEADERS = ['Received', 'Ko-fi time', 'Type', 'Name', 'Amount', 'Currency', 'Message', 'Public',
                 'Transaction ID', 'Message ID', 'Tracker', 'Forwarded'];

function doPost(e) {
  const raw = e && e.parameter && e.parameter.data;  // Ko-fi posts a form with one field, "data", holding JSON
  if (!raw) return reply('no data');
  return reply(handle(raw, true));
}

function doGet() {
  return reply('The Ko-fi inbox is running. Ko-fi sends donations here; opening this page does nothing.');
}

/**
 * Pass the donation on (if FORWARD_TO is set), then record it on the Ko-fi tab.
 * Passing it on comes first, and nothing that happens on the sheet can stop it.
 */
function handle(raw, passOn) {
  const lock = LockService.getScriptLock();
  const locked = lock.tryLock(30000);  // donations arriving together are handled one at a time
  try {
    let d = null;
    try {
      d = JSON.parse(raw);
    } catch (err) {
      // still passed on below: the receiving service decides what to do with it
    }
    const messageId = d && d.message_id ? String(d.message_id) : '';
    const forwarded = passOn ? forward(raw, messageId) : (FORWARD_TO ? 'not sent (test from the editor)' : '');

    if (!d) return 'not JSON';
    if (d.verification_token !== VERIFICATION_TOKEN) return 'wrong verification token';
    try {
      const sheet = inbox();
      if (messageId && seenOnSheet(sheet, messageId)) return 'duplicate';  // Ko-fi sent this one before
      const isPublic = d.is_public !== false;  // a private donation keeps its name and message off the sheet
      sheet.appendRow([
        new Date(),
        text(d.timestamp),
        text(d.type),
        isPublic ? text(d.from_name) : '',
        Number(d.amount) || 0,
        text(d.currency),
        isPublic ? text(d.message) : '',
        isPublic ? 'yes' : 'no',
        text(d.kofi_transaction_id),
        text(messageId),
        '',  // Tracker: the tracker fills this in once it has counted the row
        forwarded,
      ]);
      return 'ok';  // the supporter's email address is never stored
    } catch (err) {
      console.error('Could not record the donation: ' + err);
      return 'not recorded';
    }
  } finally {
    if (locked) lock.releaseLock();
  }
}

/**
 * Send Ko-fi's message on to FORWARD_TO exactly as received. A donation already passed on (Ko-fi resending it)
 * is not passed on again, so a timer widget never gets the same donation twice.
 */
function forward(raw, messageId) {
  if (!FORWARD_TO) return '';
  const cache = CacheService.getScriptCache();
  const key = messageId ? 'forwarded:' + messageId : '';
  if (key && cache.get(key)) return 'already sent';
  if (messageId) {
    try {
      if (seenOnSheet(inbox(), messageId)) return 'already sent';
    } catch (err) {
      // the sheet is unreachable: pass it on anyway
    }
  }
  try {
    const response = UrlFetchApp.fetch(FORWARD_TO, {method: 'post', payload: {data: raw}, muteHttpExceptions: true});
    const code = response.getResponseCode();
    if (key && code < 500) cache.put(key, String(code), 21600);  // remembered for 6 hours
    return code >= 200 && code < 300 ? 'sent' : 'sent, but it answered ' + code;
  } catch (err) {
    return 'failed: ' + err;  // for example, that service is down
  }
}

/** Run from the editor (choose testDonation, press Run) to check that a row appears; then type skip in its Tracker column.
 *  It is not passed on to FORWARD_TO. */
function testDonation() {
  const result = handle(JSON.stringify({
    verification_token: VERIFICATION_TOKEN,
    message_id: 'test-' + Date.now(),
    timestamp: new Date().toISOString(),
    type: 'Donation',
    is_public: true,
    from_name: 'Script_Test',
    message: 'test from the script editor',
    amount: '1.00',
    currency: 'USD',
    kofi_transaction_id: 'test',
  }), false);
  Logger.log(result);
}

function inbox() {
  const book = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = book.getSheetByName(TAB_NAME);
  if (!sheet) {
    sheet = book.insertSheet(TAB_NAME);
    sheet.appendRow(HEADERS);
    sheet.setFrozenRows(1);
    sheet.getRange('I:J').setNumberFormat('@');  // IDs stay text
  } else if (sheet.getRange(1, HEADERS.length).getValue() === '') {
    sheet.getRange(1, HEADERS.length).setValue(HEADERS[HEADERS.length - 1]);  // a tab made before Forwarded existed
  }
  return sheet;
}

function seenOnSheet(sheet, messageId) {
  const last = sheet.getLastRow();
  if (last < 2) return false;
  const ids = sheet.getRange(2, 10, last - 1, 1).getDisplayValues();
  for (let i = 0; i < ids.length; i++) {
    if (ids[i][0] === messageId) return true;
  }
  return false;
}

/** Text exactly as typed: a donor name starting with = + - or @ must not become a formula. */
function text(value) {
  const s = value === null || value === undefined ? '' : String(value);
  return /^[=+\-@]/.test(s) ? "'" + s : s;
}

function reply(message) {
  return ContentService.createTextOutput(message).setMimeType(ContentService.MimeType.TEXT);
}

/**
 * Ko-fi -> Google Sheet, for the Twitch Gift Tracker.
 *
 * Ko-fi sends every donation to this script's web address (a "webhook"). The script writes each one as a row
 * on a "Ko-fi" tab in this spreadsheet; the tracker reads that tab and adds the amounts to Dollars (column F).
 * Donations made while the tracker is off wait on the tab until it runs again.
 *
 * Setup (details in README.md, "Ko-fi and Streamlabs donations"):
 *   1. In the spreadsheet: Extensions > Apps Script. Replace everything in the editor with this file. Save.
 *   2. Paste the Ko-fi Verification Token below (Ko-fi: Settings > More > API > Advanced).
 *   3. Deploy > New deployment > Web app. Execute as: Me. Who has access: Anyone. Deploy, then allow access.
 *   4. Give the Web app URL (it ends in /exec) to the streamer to paste into Ko-fi's webhook box.
 *
 * The token belongs only in the copy inside the spreadsheet; keep the copy on GitHub as it is.
 */

const VERIFICATION_TOKEN = 'PASTE_THE_KOFI_VERIFICATION_TOKEN_HERE';
const TAB_NAME = 'Ko-fi';
const HEADERS = ['Received', 'Ko-fi time', 'Type', 'Name', 'Amount', 'Currency', 'Message', 'Public',
                 'Transaction ID', 'Message ID', 'Tracker'];

function doPost(e) {
  const raw = e && e.parameter && e.parameter.data;  // Ko-fi posts a form with one field, "data", holding JSON
  if (!raw) return reply('no data');
  let d;
  try {
    d = JSON.parse(raw);
  } catch (err) {
    return reply('not JSON');
  }
  if (d.verification_token !== VERIFICATION_TOKEN) return reply('wrong verification token');

  const lock = LockService.getScriptLock();  // two donations at once must not both pass the duplicate check
  lock.waitLock(30000);
  try {
    const sheet = inbox();
    const messageId = String(d.message_id || '');
    const last = sheet.getLastRow();
    if (messageId && last > 1) {
      const ids = sheet.getRange(2, 10, last - 1, 1).getDisplayValues().map(function (r) { return r[0]; });
      if (ids.indexOf(messageId) !== -1) return reply('duplicate');  // Ko-fi sent this one before
    }
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
    ]);
    return reply('ok');  // the supporter's email address is never stored
  } finally {
    lock.releaseLock();
  }
}

function doGet() {
  return reply('The Ko-fi inbox is running. Ko-fi sends donations here; opening this page does nothing.');
}

/** Run from the editor (choose testDonation, press Run) to check that a row appears; then type skip in its Tracker column. */
function testDonation() {
  const result = doPost({parameter: {data: JSON.stringify({
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
  })}});
  Logger.log(result.getContent());
}

function inbox() {
  const book = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = book.getSheetByName(TAB_NAME);
  if (!sheet) {
    sheet = book.insertSheet(TAB_NAME);
    sheet.appendRow(HEADERS);
    sheet.setFrozenRows(1);
    sheet.getRange('I:J').setNumberFormat('@');  // IDs stay text
  }
  return sheet;
}

/** Text exactly as typed: a donor name starting with = + - or @ must not become a formula. */
function text(value) {
  const s = value === null || value === undefined ? '' : String(value);
  return /^[=+\-@]/.test(s) ? "'" + s : s;
}

function reply(message) {
  return ContentService.createTextOutput(message).setMimeType(ContentService.MimeType.TEXT);
}

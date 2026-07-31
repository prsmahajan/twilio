// Drives the added views in a real browser: follow-ups, recordings, SMS
// templates, and the enrichment/timeline panel on a lead.
import { chromium } from 'playwright';

const BASE = process.env.BASE || 'http://localhost:5099';
const fails = [];
const ck = (l, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${l}: ${JSON.stringify(got)}`);
  if (!ok) { fails.push(l); console.log(`      wanted ${JSON.stringify(want)}`); }
};

const browser = await chromium.launch();
const page = await (await browser.newContext()).newPage();
const errors = [];
page.on('pageerror', e => errors.push(e.message));
page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

await page.goto(BASE, { waitUntil: 'networkidle' });
await page.waitForTimeout(1400);

// Twilio's device cannot register against fake credentials in a test, and that
// noise is expected; anything else is a real regression.
const real = errors.filter(e =>
  !/AccessTokenInvalid|3120[0-9]|Twilio|websocket|WebSocket|register|Failed to load resource/i.test(e));
ck('no JS errors on load', real, []);

console.log('\n=== seed a lead ===');
await page.click('.tab[data-tab="leads"]');
await page.waitForTimeout(300);
await page.click('#btn-leads-import');
await page.waitForTimeout(300);
await page.fill('#import-csv', 'name,phone,company\nAda Lovelace,2125551212,Analytical\n');
await page.click('#btn-import-csv');
await page.waitForTimeout(900);
await page.click('#btn-back-leads3');
await page.waitForTimeout(500);

console.log('\n=== More menu reaches the new views ===');
await page.click('.tab[data-tab="more"]');
await page.waitForTimeout(300);
for (const [row, view] of [['tasks', 'view-tasks'],
                           ['templates', 'view-templates'],
                           ['recordings', 'view-recordings']]) {
  await page.click('.tab[data-tab="more"]');
  await page.waitForTimeout(250);
  await page.click(`[data-goto="${row}"]`);
  await page.waitForTimeout(500);
  ck(`${row} view opens`, await page.isVisible(`#${view}`), true);
}

console.log('\n=== follow-ups ===');
await page.click('.tab[data-tab="more"]');
await page.waitForTimeout(250);
await page.click('[data-goto="tasks"]');
await page.waitForTimeout(400);
ck('empty state shown', /Nothing scheduled/.test(await page.textContent('#tasks-list')), true);

await page.click('#btn-task-new');
await page.waitForTimeout(300);
ck('sheet opens', await page.isVisible('#task-title'), true);
await page.fill('#task-title', 'Ring Ada back');
await page.fill('#task-due', '+2h');
await page.click('#btn-task-save');
await page.waitForTimeout(700);
ck('task listed', (await page.$$('.task-row')).length, 1);
ck('task title rendered', await page.textContent('.task-title'), 'Ring Ada back');

// An overdue task has to be visibly different or the list is just a pile.
await page.click('[data-task-scope="overdue"]');
await page.waitForTimeout(500);
ck('future task is not overdue', (await page.$$('.task-row')).length, 0);
await page.click('[data-task-scope="open"]');
await page.waitForTimeout(500);

await page.click('.task-check');
await page.waitForTimeout(600);
ck('completing removes it from open', (await page.$$('.task-row')).length, 0);
await page.click('[data-task-scope="done"]');
await page.waitForTimeout(500);
ck('completed task in done scope', (await page.$$('.task-row')).length, 1);

await page.click('.task-del');
await page.waitForTimeout(600);
ck('task deleted', (await page.$$('.task-row')).length, 0);

console.log('\n=== SMS templates ===');
await page.click('.tab[data-tab="more"]');
await page.waitForTimeout(250);
await page.click('[data-goto="templates"]');
await page.waitForTimeout(400);
ck('merge-field hint shown',
   /\{\{first_name\}\}/.test(await page.textContent('#tpl-merge-hint')), true);

// A message box has to show its remaining budget and refuse to hold more than
// one SMS segment, because the server rejects anything longer.
ck('template body counter shown', await page.isVisible('#tpl-body-count'), true);
await page.fill('#tpl-body', 'x'.repeat(150));
await page.waitForTimeout(150);
ck('counter counts up', await page.textContent('#tpl-body-count'), '150/160');
ck('counter warns near the cap',
   await page.$eval('#tpl-body-count', e => e.classList.contains('near')), true);
await page.fill('#tpl-body', 'x'.repeat(400));
await page.waitForTimeout(150);
ck('maxlength truncates at one segment',
   (await page.inputValue('#tpl-body')).length, 160);

await page.fill('#tpl-name', 'Intro');
await page.fill('#tpl-body', 'Hi {{first_name}}, quick question.');
await page.click('#btn-tpl-save');
await page.waitForTimeout(700);
ck('template saved', (await page.$$('.tpl-row')).length, 1);
ck('form cleared after save', await page.inputValue('#tpl-name'), '');

// The picker above the SMS composer is the point of templates existing.
await page.click('.tab[data-tab="messages"]');
await page.waitForTimeout(600);
const opts = await page.$$eval('#sms-template option', els => els.map(e => e.textContent));
ck('template offered in the composer', opts.includes('Intro'), true);
ck('composer counter shown', await page.isVisible('#sms-body-count'), true);

console.log('\n=== lead detail: badges, window notice, timeline ===');
await page.click('.tab[data-tab="leads"]');
await page.waitForTimeout(500);
await page.click('.lead-item');
await page.waitForTimeout(800);
ck('detail opened', await page.textContent('#ld-name'), 'Ada Lovelace');
ck('timezone badge from area code',
   /New York/.test(await page.textContent('#ld-badges')), true);
ck('enrich button present', await page.isVisible('#btn-ld-enrich'), true);
ck('timeline rendered', await page.isVisible('#ld-timeline'), true);

await page.click('#btn-ld-task');
await page.waitForTimeout(300);
ck('task sheet knows the lead',
   /open lead/.test(await page.textContent('#task-for')), true);
await page.fill('#task-title', 'Send pricing');
await page.click('#btn-task-save');
await page.waitForTimeout(800);
ck('task shows on the lead timeline',
   /Send pricing/.test(await page.textContent('#ld-timeline')), true);

console.log('\n=== data actions ===');
await page.click('.tab[data-tab="more"]');
await page.waitForTimeout(300);
for (const id of ['btn-export-leads', 'btn-export-calls', 'btn-enrich-all']) {
  ck(`${id} present`, await page.isVisible(`#${id}`), true);
}

const download = await Promise.all([
  page.waitForEvent('download', { timeout: 8000 }).catch(() => null),
  page.click('#btn-export-leads'),
]).then(([d]) => d);
ck('leads CSV downloads', download ? download.suggestedFilename().endsWith('.csv') : false, true);

const late = errors.filter(e =>
  !/AccessTokenInvalid|3120[0-9]|Twilio|websocket|WebSocket|register|Failed to load resource/i.test(e));
ck('no JS errors during the run', late, []);

await browser.close();
console.log(fails.length ? `\n${fails.length} FAILED: ${fails}` : '\nALL PASSED');
process.exit(fails.length ? 1 : 0);

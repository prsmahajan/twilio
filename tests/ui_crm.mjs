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

const real = errors.filter(e => !/AccessTokenInvalid|3120[0-9]|Twilio|websocket|WebSocket|register|Failed to load resource/i.test(e));
ck('no JS errors on load', real, []);

console.log('\n=== tabs ===');
for (const t of ['leads', 'campaigns', 'messages', 'more', 'keypad']) {
  await page.click(`.tab[data-tab="${t}"]`);
  await page.waitForTimeout(350);
  ck(`tab ${t} activates`, await page.isVisible(`#view-${t}`), true);
}

console.log('\n=== import via pasted CSV ===');
await page.click('.tab[data-tab="leads"]');
await page.waitForTimeout(400);
await page.click('#btn-leads-import');
await page.waitForTimeout(400);
ck('import view open', await page.isVisible('#import-csv'), true);

await page.fill('#import-csv', 'name,phone,company\nAda Lovelace,5551110001,Analytical\nGrace Hopper,5551110002,Navy\nBad Row,xxx,Nope\n');
await page.click('#btn-import-csv');
await page.waitForTimeout(900);
const rep = await page.textContent('#import-result');
ck('reports 2 imported', /2<\/b> imported|2 imported/.test(rep.replace(/\s+/g, ' ')), true);
ck('reports 1 unreadable', /1 unreadable/.test(rep), true);

console.log('\n=== bad Google Sheet URL surfaces a real message ===');
await page.fill('#import-sheet-url', 'https://evil.example.com/spreadsheets/d/x');
await page.click('#btn-import-sheet');
await page.waitForTimeout(700);
ck('sheet error shown', /Google Sheets URL/.test(await page.textContent('#import-result')), true);

console.log('\n=== leads list + search ===');
await page.click('#btn-back-leads3');
await page.waitForTimeout(450);
const cnt = await page.textContent('#leads-count');
ck('lead count rendered', /\d+ leads?/.test(cnt), true);
ck('leads listed', (await page.$$('.lead-item')).length >= 2, true);

await page.fill('#leads-search', 'Grace');
await page.waitForTimeout(600);
ck('search narrows to 1', (await page.$$('.lead-item')).length, 1);
await page.fill('#leads-search', '');
await page.waitForTimeout(600);

console.log('\n=== lead detail ===');
// Target Grace explicitly so the DNC assertions below are order-independent.
await page.fill('#leads-search', 'Grace');
await page.waitForTimeout(600);
await page.click('.lead-item');
await page.waitForTimeout(500);
ck('detail opens', await page.isVisible('#ld-name'), true);
ck('opened the right lead', await page.textContent('#ld-name'), 'Grace Hopper');
ck('phone formatted', /\(\d{3}\) \d{3}-\d{4}/.test(await page.textContent('#ld-phone')), true);
ck('history section rendered', await page.isVisible('#ld-history'), true);

// Mark DNC and save
await page.check('#ld-dnc');
await page.click('#btn-ld-save');
await page.waitForTimeout(700);
await page.fill('#leads-search', 'Grace');
await page.waitForTimeout(600);
const dncBadge = await page.$$eval('.lead-item .disp', els => els.map(e => e.textContent));
ck('DNC badge appears in list', dncBadge.includes('DNC'), true);
await page.fill('#leads-search', '');
await page.waitForTimeout(600);

console.log('\n=== campaign create + queue ===');
await page.click('.tab[data-tab="campaigns"]');
await page.waitForTimeout(450);
await page.click('#btn-camp-new');
await page.waitForTimeout(450);
ck('available lead count shown', /\d+ leads?/.test(await page.textContent('#nc-lead-count')), true);

await page.fill('#nc-name', 'Playwright Campaign');
await page.fill('#nc-gap', '1');
await page.click('#btn-create-camp');
await page.waitForTimeout(1100);
ck('campaign run view opens', await page.isVisible('#cr-queue'), true);
ck('campaign name shown', await page.textContent('#cr-name'), 'Playwright Campaign');

const qlen = (await page.$$('#cr-queue .auto-num-item')).length;
ck('queue populated', qlen >= 1, true);

// The DNC lead must be excluded from the queue entirely.
const queueText = await page.textContent('#cr-queue');
ck('DNC lead excluded from queue', /Grace/.test(queueText), false);
ck('progress line shown', /\d+ of \d+ done/.test(await page.textContent('#cr-progress')), true);

console.log('\n=== disposition sheet ===');
await page.evaluate(() => window.__crm.promptDisposition('CA_ui_test', '(555) 111-0001'));
await page.waitForTimeout(450);
ck('sheet opens', await page.isVisible('#disp-overlay'), true);
ck('shows number', await page.textContent('#disp-for'), '(555) 111-0001');
const opts = await page.$$eval('.disp-opt', e => e.map(x => x.textContent));
ck('six outcome buttons', opts.length, 6);
ck('includes Interested', opts.includes('Interested'), true);
ck('includes Do not call', opts.includes('Do not call'), true);

await page.click('#btn-disp-skip');
await page.waitForTimeout(350);
ck('skip closes sheet', await page.isVisible('#disp-overlay'), false);

console.log('\n=== delete all leads ===');
await page.click('.tab[data-tab="leads"]');
await page.waitForTimeout(600);
const beforeCount = (await page.$$('.lead-item')).length;
ck('leads present before wipe', beforeCount >= 2, true);

await page.click('#btn-leads-more');
await page.waitForTimeout(700);
ck('confirm sheet opens', await page.isVisible('#wipe-overlay'), true);
ck('summary mentions counts', /\d+ of \d+ leads? will be deleted/.test(await page.textContent('#wipe-summary')), true);
ck('delete disarmed initially', await page.getAttribute('#btn-wipe-go', 'disabled'), '');

await page.fill('#wipe-confirm', 'delete');
await page.waitForTimeout(200);
ck('wrong case stays disarmed', await page.getAttribute('#btn-wipe-go', 'disabled'), '');

await page.fill('#wipe-confirm', 'DELETE');
await page.waitForTimeout(200);
ck('exact match arms it', await page.getAttribute('#btn-wipe-go', 'disabled'), null);

await page.click('#btn-wipe-go');
await page.waitForTimeout(1400);
ck('sheet closes', await page.isVisible('#wipe-overlay'), false);
// Grace was marked do-not-call earlier, so she must survive.
const after = await page.$$eval('.lead-item', els => els.map(e => e.textContent));
ck('dnc lead preserved', after.some(t => /Grace/.test(t)), true);
ck('non-dnc leads gone', after.some(t => /Ada/.test(t)), false);

console.log('\n=== analytics ===');
await page.click('.tab[data-tab="more"]');
await page.waitForTimeout(400);
await page.click('#view-more [data-goto="analytics"]');
await page.waitForTimeout(900);
ck('analytics view open', await page.isVisible('#analytics-body'), true);
const stats = await page.$$eval('.stat-n', e => e.map(x => x.textContent));
// Calls, connect rate, talk time, avg call, texts sent, reply rate, texts failed,
// recordings, spend, call spend, text spend, cost per connect, texts delivered,
// leads, do-not-call, follow-ups, overdue.
ck('seventeen stat tiles', stats.length, 17);
ck('stats are populated', stats.every(s => s.length > 0), true);

// Every section the view renders, so a broken payload key is caught here rather
// than showing up as a silently missing block.
const heads = await page.$$eval('#analytics-body .sub-head', e => e.map(x => x.textContent.trim()));
ck('funnel rendered', heads.includes('Lead funnel'), true);
ck('call results rendered', heads.includes('Call results'), true);
ck('lead stages rendered', heads.includes('Lead stages'), true);
ck('line types rendered', heads.includes('Line types'), true);
ck('funnel has four steps', await page.locator('.funnel-row').count(), 4);

const labels = await page.$$eval('#analytics-body .stat-l', e => e.map(x => x.textContent));
ck('spend reported', labels.includes('Spend'), true);
ck('cost per connect reported', labels.includes('Cost per connect'), true);
ck('delivery reported', labels.includes('Texts delivered'), true);

// Prices arrive from Twilio minutes after a call, so the sync is explicit.
await page.click('#btn-analytics-costs');
await page.waitForTimeout(1400);
ck('cost sync reports back', /Priced|failed/.test(await page.textContent('#status-bar')), true);

console.log('\n=== analytics filters ===');
await page.selectOption('#analytics-range', '1');
await page.waitForTimeout(600);
ck('today range labelled', (await page.textContent('#analytics-body')).includes('Today'), true);

await page.selectOption('#analytics-range', 'custom');
await page.waitForTimeout(300);
ck('custom dates revealed', await page.isVisible('#analytics-custom'), true);
await page.fill('#analytics-from', '2026-01-01');
await page.fill('#analytics-to', '2026-01-10');
await page.waitForTimeout(700);
ck('custom range labelled',
   (await page.textContent('#analytics-body')).includes('2026-01-01 to 2026-01-10'), true);

// The campaign filter is filled from the campaign list, so the campaign created
// earlier in this suite has to be selectable.
const campaignOpts = await page.$$eval('#analytics-campaign option', e => e.map(x => x.textContent));
ck('campaign filter populated', campaignOpts.length > 1, true);

await page.selectOption('#analytics-range', '14');
await page.waitForTimeout(600);

console.log('\n=== More menu navigation ===');
await page.click('#view-analytics [data-back="more"]');
await page.waitForTimeout(400);
await page.click('#view-more [data-goto="settings"]');
await page.waitForTimeout(400);
ck('settings reachable from More', await page.isVisible('#set-identity'), true);
ck('More tab stays highlighted', await page.getAttribute('.tab[data-tab="more"]', 'class'), 'tab active');

await page.screenshot({ path: '/tmp/crm_final.png' });

const real2 = errors.filter(e => !/AccessTokenInvalid|3120[0-9]|Twilio|websocket|WebSocket|register|Failed to load resource/i.test(e));
ck('no JS errors after full walk', real2, []);

console.log('\n' + (fails.length ? `${fails.length} FAILED: ${fails}` : 'ALL PASSED'));
await browser.close();
process.exit(fails.length ? 1 : 0);

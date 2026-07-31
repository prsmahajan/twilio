import { chromium } from 'playwright';

const BASE = process.env.BASE || 'http://localhost:5099';
const fails = [];
const ck = (label, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}: ${JSON.stringify(got)}`);
  if (!ok) { fails.push(label); console.log(`      wanted ${JSON.stringify(want)}`); }
};

const browser = await chromium.launch();
const ctx = await browser.newContext({ permissions: [] });
const page = await ctx.newPage();

const errors = [];
page.on('pageerror', e => errors.push(e.message));
page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

await page.goto(BASE, { waitUntil: 'networkidle' });
await page.waitForTimeout(1500);

console.log('=== page load ===');
ck('title', await page.title(), 'Dialer');
// Device.register() will fail against fake creds; that is expected and not a page error.
const realErrors = errors.filter(e => !/AccessTokenInvalid|31204|31205|Twilio|websocket|WebSocket|register/i.test(e));
ck('no unexpected JS errors', realErrors, []);

console.log('\n=== keypad ===');
await page.click('.key[data-digit="5"]');
await page.click('.key[data-digit="5"]');
await page.click('.key[data-digit="5"]');
ck('digits entered', await page.textContent('#display'), '555');

// Long-press 0 should insert '+', not '0'
await page.dispatchEvent('.key[data-digit="0"]', 'pointerdown');
await page.waitForTimeout(700);
await page.dispatchEvent('.key[data-digit="0"]', 'pointerup');
ck('long-press 0 inserts +', await page.textContent('#display'), '555+');

// Short tap 0 should insert '0'
await page.dispatchEvent('.key[data-digit="0"]', 'pointerdown');
await page.waitForTimeout(80);
await page.dispatchEvent('.key[data-digit="0"]', 'pointerup');
ck('short tap 0 inserts 0', await page.textContent('#display'), '555+0');

// Backspace long-press clears
await page.dispatchEvent('#btn-backspace', 'pointerdown');
await page.waitForTimeout(700);
await page.dispatchEvent('#btn-backspace', 'pointerup');
ck('long-press backspace clears', await page.textContent('#display'), '');

console.log('\n=== callee local time ===');
// The clock is what stops a 9pm dial landing at 4am for the callee, so it has to
// appear for a domestic number and for an international one, and name the place.
await page.dispatchEvent('#btn-backspace', 'pointerdown');
await page.waitForTimeout(700);
await page.dispatchEvent('#btn-backspace', 'pointerup');

for (const d of '12125551212') await page.click(`.key[data-digit="${d}"]`);
await page.waitForTimeout(1200);
ck('clock shown for a US number', await page.isVisible('#dial-clock'), true);
const usClock = await page.textContent('#dial-clock');
ck('clock names the country', /US\/Canada/.test(usClock), true);
ck('clock shows a time', /\d{2}:\d{2}:\d{2}/.test(usClock), true);

// It must tick, not freeze on the value the server sent.
const firstTick = (await page.textContent('#dial-clock')).match(/\d{2}:\d{2}:\d{2}/)[0];
await page.waitForTimeout(1600);
const secondTick = (await page.textContent('#dial-clock')).match(/\d{2}:\d{2}:\d{2}/)[0];
ck('clock is live', firstTick !== secondTick, true);

await page.dispatchEvent('#btn-backspace', 'pointerdown');
await page.waitForTimeout(700);
await page.dispatchEvent('#btn-backspace', 'pointerup');
await page.waitForTimeout(400);
ck('clock cleared with the number', await page.isVisible('#dial-clock'), false);

// An Indian number resolves from its country code alone.
await page.dispatchEvent('.key[data-digit="0"]', 'pointerdown');
await page.waitForTimeout(700);
await page.dispatchEvent('.key[data-digit="0"]', 'pointerup');
for (const d of '919876543210') await page.click(`.key[data-digit="${d}"]`);
await page.waitForTimeout(1200);
ck('clock names India', /India/.test(await page.textContent('#dial-clock')), true);

await page.dispatchEvent('#btn-backspace', 'pointerdown');
await page.waitForTimeout(700);
await page.dispatchEvent('#btn-backspace', 'pointerup');
await page.waitForTimeout(300);

console.log('\n=== invalid number rejected before dialing ===');
await page.click('.key[data-digit="1"]');
await page.click('#btn-call');
await page.waitForTimeout(400);
const st = await page.textContent('#status-bar');
ck('short number refused', /Invalid number/.test(st), true);

console.log('\n=== navigation ===');
await page.click('.tab[data-tab="messages"]');
ck('messages view active', await page.isVisible('#view-messages .msg-header'), true);
await page.click('.tab[data-tab="more"]');
await page.waitForTimeout(400);
await page.click('#view-more [data-goto="settings"]');
await page.waitForTimeout(450);
ck('settings view active', await page.isVisible('#set-identity'), true);
ck('settings shows identity', await page.textContent('#set-identity'), 'dialer-user');
await page.click('.tab[data-tab="keypad"]');
await page.waitForTimeout(400);

console.log('\n=== autodialer parsing in the real UI ===');
await page.click('#btn-autodialer');
await page.waitForTimeout(450);
await page.fill('#auto-input', '5551234567\n(555) 987-6543\n+442071234567\ngarbage\n5551234567');
await page.waitForTimeout(300);
const count = await page.textContent('#auto-digit-count');
ck('counts valid + flags invalid + dedupes', count, '3 numbers · 1 unrecognized');

console.log('\n=== in-call bar hidden when idle ===');
ck('in-call bar hidden', await page.isVisible('#in-call-bar'), false);
ck('incoming overlay hidden', await page.isVisible('#incoming-overlay'), false);

console.log('\n=== simulate an active call to verify in-call controls ===');
// Drive the UI directly with a fake call object shaped like a Voice SDK Call.
await page.evaluate(() => {
  const handlers = {};
  window.__fake = {
    muted: false,
    digits: [],
    on: (e, cb) => { (handlers[e] = handlers[e] || []).push(cb); },
    emit: (e, a) => (handlers[e] || []).forEach(cb => cb(a)),
    isMuted() { return this.muted; },
    mute(v) { this.muted = v; },
    sendDigits(d) { this.digits.push(d); },
    disconnect() { this.emit('disconnect'); },
  };
  window.__attach(window.__fake, '+15551234567');
  window.__fake.emit('accept');
});
await page.waitForTimeout(1200);

ck('in-call bar visible', await page.isVisible('#in-call-bar'), true);
ck('DTMF hint shown on connect', /Keypad now sends tones/.test(await page.textContent('#dtmf-sent')), true);
ck('hint is styled as a hint', await page.getAttribute('#dtmf-sent', 'class'), 'dtmf-sent hint');
ck('display hint explains tones', await page.textContent('#dial-hint'), 'Tones go to the person you called');
const timer = await page.textContent('#call-timer');
ck('call timer running', /^00:0[0-9]$/.test(timer), true);

await page.click('#btn-mute');
ck('mute engaged', await page.evaluate(() => window.__fake.muted), true);
ck('mute button lit', await page.getAttribute('#btn-mute', 'class'), 'call-ctrl on');
await page.click('#btn-mute');
ck('unmute', await page.evaluate(() => window.__fake.muted), false);

// During a call the main keypad sends DTMF instead of editing the dial string.
const before = await page.textContent('#display');
for (const d of ['1', '2', '#']) {
  await page.dispatchEvent(`.key[data-digit="${d}"]`, 'pointerdown');
  await page.dispatchEvent(`.key[data-digit="${d}"]`, 'pointerup');
}
ck('keypad sends DTMF on call', await page.evaluate(() => window.__fake.digits), ['1', '2', '#']);
ck('dtmf echo shown', await page.textContent('#dtmf-sent'), 'Sent  1 2 #');
ck('hint style cleared once sending', await page.getAttribute('#dtmf-sent', 'class'), 'dtmf-sent');
ck('display untouched during call', await page.textContent('#display'), before);

await page.click('#btn-hangup-bar');
await page.waitForTimeout(300);
ck('in-call bar hides after hangup', await page.isVisible('#in-call-bar'), false);

console.log('\n=== simulate an incoming call ===');
await page.evaluate(() => {
  const handlers = {};
  const call = {
    parameters: { From: '+15559876543' },
    on: (e, cb) => { (handlers[e] = handlers[e] || []).push(cb); },
    emit: (e) => (handlers[e] || []).forEach(cb => cb()),
    accept() { window.__accepted = true; },
    reject() { window.__rejected = true; this.emit('reject'); },
  };
  window.__incoming = call;
  window.__handleIncoming(call);
});
await page.waitForTimeout(300);
ck('incoming overlay shown', await page.isVisible('#incoming-overlay'), true);
ck('caller number formatted', await page.textContent('#inc-number'), '(555) 987-6543');

await page.click('#btn-reject');
await page.waitForTimeout(300);
ck('reject called', await page.evaluate(() => window.__rejected), true);
ck('overlay dismissed', await page.isVisible('#incoming-overlay'), false);

console.log('\n=== screenshot ===');
await page.click('.tab[data-tab="keypad"]');
await page.screenshot({ path: '/tmp/dialer_keypad.png' });

console.log('\n' + (fails.length ? `${fails.length} FAILED: ${fails}` : 'ALL PASSED'));
await browser.close();
process.exit(fails.length ? 1 : 0);

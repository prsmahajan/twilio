// Geist, per the design system. Bundled locally rather than pulled from a CDN
// so the app keeps its typography offline and behind a strict CSP.
// Three weights only: 400 read, 500 interact, 600 announce.
import '@fontsource/geist-sans/400.css';
import '@fontsource/geist-sans/500.css';
import '@fontsource/geist-sans/600.css';
import '@fontsource/geist-mono/400.css';
import '@fontsource/geist-mono/500.css';

import { Device } from '@twilio/voice-sdk';
import { esc, fmtDate, fmtDur, normalizeNumber, formatNumber, api, json, scrollIntoParent } from './lib.js';
import * as crm from './crm.js';
import * as extras from './extras.js';
import { createCallClock } from './calltiming.js';

// ── DOM refs ──────────────────────────────────────────────────────────────────
const statusBar       = document.getElementById('status-bar');
const displayEl       = document.getElementById('display');
const displayRowEl    = document.getElementById('display-row');
const dialHintEl      = document.getElementById('dial-hint');
const dialClockEl     = document.getElementById('dial-clock');
const btnBackspace    = document.getElementById('btn-backspace');
const btnCall         = document.getElementById('btn-call');
const btnAutodialer   = document.getElementById('btn-autodialer');
const btnBackAuto     = document.getElementById('btn-back-auto');
const autoInput       = document.getElementById('auto-input');
const autoDigitCount  = document.getElementById('auto-digit-count');
const autoListEl      = document.getElementById('auto-list');
const autoProgress    = document.getElementById('auto-progress');
const autoSummaryEl   = document.getElementById('auto-summary');
const autoGapEl       = document.getElementById('auto-gap');
const btnStartAuto    = document.getElementById('btn-start-auto');
const btnCancelAuto   = document.getElementById('btn-cancel-auto');
const btnPauseAuto    = document.getElementById('btn-pause-auto');
const btnStopAuto     = document.getElementById('btn-stop-auto');
const btnSkipAuto     = document.getElementById('btn-skip-auto');
const autoCtrlIdle    = document.getElementById('auto-ctrl-idle');
const autoCtrlRunning = document.getElementById('auto-ctrl-running');
const threadsListEl   = document.getElementById('threads-list');
const btnCompose      = document.getElementById('btn-compose');
const btnBackConv     = document.getElementById('btn-back-conv');
const convContactEl   = document.getElementById('conv-contact');
const convMessagesEl  = document.getElementById('conv-messages');
const smsBodyEl       = document.getElementById('sms-body');
const btnSendSms      = document.getElementById('btn-send-sms');
const btnCallContact  = document.getElementById('btn-call-contact');
const btnBackCompose  = document.getElementById('btn-back-compose');
const smsToEl         = document.getElementById('sms-to');
const smsTextEl       = document.getElementById('sms-text');
const btnSendNewSms   = document.getElementById('btn-send-new-sms');
const recentListEl    = document.getElementById('recent-list');

const inCallBar       = document.getElementById('in-call-bar');
const callTimerEl     = document.getElementById('call-timer');
const btnMute         = document.getElementById('btn-mute');
const btnHangupBar    = document.getElementById('btn-hangup-bar');
const dtmfSentEl      = document.getElementById('dtmf-sent');

const incomingOverlay = document.getElementById('incoming-overlay');
const incNumberEl     = document.getElementById('inc-number');
const btnAccept       = document.getElementById('btn-accept');
const btnReject       = document.getElementById('btn-reject');

const setIdentityEl   = document.getElementById('set-identity');
const setDevStatusEl  = document.getElementById('set-devstatus');
const setTokenExpEl   = document.getElementById('set-tokenexp');
const setMicEl        = document.getElementById('set-mic');
const setSpkEl        = document.getElementById('set-spk');
const setIncomingEl   = document.getElementById('set-incoming');
const btnTestAudio    = document.getElementById('btn-test-audio');

// ── State ─────────────────────────────────────────────────────────────────────
let device       = null;
let activeCall   = null;
let pendingCall  = null;   // ringing inbound call awaiting accept/reject
let tokenExpiry  = null;
let refreshTimer = null;

let callTimerInt = null;
let callStartMs  = null;
let dtmfBuffer   = '';

// One list of { number, status } — replaces the old duplicated queue arrays.
const auto = { items: [], index: 0, running: false, paused: false, timer: null };
let autoAnswered = false;   // did the current auto call ever connect?

// ── Status ────────────────────────────────────────────────────────────────────
function setStatus(text, cls = '') {
  statusBar.textContent = text;
  statusBar.className   = cls;
  setDevStatusEl.textContent = text;
}

// ── Dial string ───────────────────────────────────────────────────────────────
// The raw keyed-in string is kept separately from what is rendered, so the
// display can be prettified without corrupting what actually gets dialed.
let dialString = '';

// Progressive US grouping as digits arrive: 555 → (555) 123 → (555) 123-4567.
function formatDialString(s) {
  // Only group plain national digits. Anything containing +, * or # is shown
  // verbatim so those characters can never be silently dropped.
  if (!/^\d+$/.test(s)) return s;
  const d = s;
  if (d.length <= 3) return d;
  if (d.length <= 6) return `(${d.slice(0, 3)}) ${d.slice(3)}`;
  if (d.length <= 10) return `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}`;
  return s;
}

function setDial(s) {
  dialString = s;
  displayEl.textContent = formatDialString(s);
  displayRowEl.classList.toggle('has-value', s.length > 0);

  // On a live call the hint line explains the keypad's changed role instead of
  // previewing a number that is no longer being edited.
  if (activeCall) {
    dialHintEl.textContent = 'Tones go to the person you called';
    dialHintEl.style.color = '';
    return;
  }

  // Show what will actually be dialed, so a missing country code is obvious
  // before the call is placed rather than after it fails.
  if (!s) {
    dialHintEl.textContent = '';
    dialHintEl.style.color = '';
    trackCalleeClock(null);
    return;
  }
  const n = normalizeNumber(s);
  if (n) {
    dialHintEl.textContent = n;
    dialHintEl.style.color = '';
  } else {
    dialHintEl.textContent = 'Needs a country code — hold 0 for +';
    dialHintEl.style.color = 'var(--amber)';
  }
  trackCalleeClock(n);
}

// ── Callee local time ─────────────────────────────────────────────────────────
// Dialling 9pm your time can be 4am theirs. The clock above the keypad shows the
// callee's wall time as it ticks, so the mistake is visible before the call.
// Shared with the lead detail and the campaign runner — see calltiming.js.
const dialClock = createCallClock(dialClockEl);

function trackCalleeClock(e164) {
  // Every keystroke changes the number, so the lookup waits for a pause.
  dialClock.track(e164, { debounce: 350 });
}

// ── View navigation ───────────────────────────────────────────────────────────
let activeView = 'keypad';

function showView(id) {
  const el = document.getElementById('view-' + id);
  if (!el) return;

  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  el.classList.add('active');

  // Belt and braces: nothing should ever scroll the view container sideways.
  const views = el.parentElement;
  if (views.scrollLeft) views.scrollLeft = 0;
  activeView = id;

  // Every clock ticks once a second. A view you have navigated away from should
  // not be one of them, and stopping centrally covers the back buttons, the tab
  // bar and anything added later.
  crm.stopClocksOutside(id);

  const tab = TAB_OF[id] || id;
  document.querySelectorAll('.tab').forEach(t =>
    t.classList.toggle('active', t.dataset.tab === tab)
  );

  VIEW_LOADERS[id]?.();
}

// Views reachable from a bottom tab. Everything else is a pushed sub-view, and
// keeps the tab of the section it belongs to highlighted.
const TAB_OF = {
  keypad: 'keypad', autodialer: 'keypad',
  leads: 'leads', 'lead-detail': 'leads', 'lead-new': 'leads', import: 'leads',
  campaigns: 'campaigns', 'campaign-new': 'campaigns', 'campaign-run': 'campaigns',
  messages: 'messages', conversation: 'messages', compose: 'messages',
  more: 'more', recent: 'more', analytics: 'more', settings: 'more',
  tasks: 'more', recordings: 'more', templates: 'more',
};

const VIEW_LOADERS = {
  messages:  () => loadThreads(),
  recent:    () => loadRecent(),
  settings:  () => refreshAudioDevices(),
  leads:     () => crm.loadLeads(),
  campaigns: () => crm.loadCampaigns(),
  analytics: () => crm.loadAnalytics(),
  tasks:      () => extras.loadTasks(),
  recordings: () => extras.loadRecordings(),
  templates:  () => extras.loadTemplates(),
};

document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => showView(tab.dataset.tab));
});

// "More" menu rows push their target view.
document.querySelectorAll('[data-goto]').forEach(el => {
  el.addEventListener('click', () => showView(el.dataset.goto));
});

document.querySelectorAll('[data-back]').forEach(el => {
  el.addEventListener('click', () => showView(el.dataset.back));
});

btnBackAuto.addEventListener('click',    () => showView('keypad'));
btnCancelAuto.addEventListener('click',  () => showView('keypad'));
btnBackConv.addEventListener('click',    () => { showView('messages'); loadThreads(); });
btnBackCompose.addEventListener('click', () => showView('messages'));
btnAutodialer.addEventListener('click',  () => showView('autodialer'));
btnCompose.addEventListener('click',     () => showView('compose'));

// ── Keypad input ──────────────────────────────────────────────────────────────
// Long-press 0 for '+'. Bound for both pointer and touch so it works on phones.
document.querySelectorAll('.key').forEach(key => {
  let pressTimer  = null;
  let longFired   = false;
  const isZero    = key.dataset.digit === '0';

  const startPress = () => {
    longFired = false;
    // Restart the ripple animation on every press.
    key.classList.remove('pressed');
    void key.offsetWidth;
    key.classList.add('pressed');
    if (!isZero) return;
    pressTimer = setTimeout(() => {
      pressTimer = null;
      longFired  = true;
      setDial(dialString + '+');
      if (navigator.vibrate) navigator.vibrate(15);
    }, 500);
  };

  const cancelPress = () => { clearTimeout(pressTimer); pressTimer = null; };

  const commit = e => {
    e.preventDefault();
    cancelPress();
    if (longFired) { longFired = false; return; }   // '+' already inserted
    // On a live call the keypad drives the IVR instead of editing the dial string.
    if (activeCall) { sendDtmf(key.dataset.digit); return; }
    setDial(dialString + key.dataset.digit);
    playTone(key.dataset.digit);
  };

  key.addEventListener('pointerdown',   startPress);
  key.addEventListener('pointerup',     commit);
  key.addEventListener('pointercancel', cancelPress);
  key.addEventListener('pointerleave',  cancelPress);
  key.addEventListener('contextmenu',   e => e.preventDefault());
});

// Backspace: tap deletes one, long-press clears all.
let bsTimer = null, bsLong = false;
btnBackspace.addEventListener('pointerdown', () => {
  bsLong  = false;
  bsTimer = setTimeout(() => { bsLong = true; setDial(''); }, 500);
});
btnBackspace.addEventListener('pointerup', () => {
  clearTimeout(bsTimer);
  if (!bsLong) setDial(dialString.slice(0, -1));
  bsLong = false;
});
btnBackspace.addEventListener('pointerleave', () => clearTimeout(bsTimer));

// Physical keyboard input for dialer
document.addEventListener('keydown', e => {
  if (activeView !== 'keypad') return;
  const tag = document.activeElement?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA') return;

  if (/^[0-9*#+]$/.test(e.key)) {
    // While on a call, digits are DTMF rather than dial-string input.
    if (activeCall) { sendDtmf(e.key); return; }
    setDial(dialString + e.key);
    playTone(e.key);
  } else if (e.key === 'Backspace') {
    setDial(dialString.slice(0, -1));
  } else if (e.key === 'Enter') {
    btnCall.click();
  }
});

// Paste into the dial display
document.addEventListener('paste', e => {
  if (activeView !== 'keypad') return;
  const tag = document.activeElement?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA') return;
  const text = (e.clipboardData || window.clipboardData).getData('text');
  const n = normalizeNumber(text);
  if (n) { e.preventDefault(); setDial(n); }
});

// ── DTMF tones (WebAudio, no assets needed) ───────────────────────────────────
const DTMF_FREQ = {
  '1': [697, 1209], '2': [697, 1336], '3': [697, 1477],
  '4': [770, 1209], '5': [770, 1336], '6': [770, 1477],
  '7': [852, 1209], '8': [852, 1336], '9': [852, 1477],
  '*': [941, 1209], '0': [941, 1336], '#': [941, 1477],
};
let audioCtx = null;

function playTone(digit, ms = 120) {
  const pair = DTMF_FREQ[digit];
  if (!pair) return;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === 'suspended') audioCtx.resume();
    const gain = audioCtx.createGain();
    gain.gain.setValueAtTime(0.09, audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + ms / 1000);
    gain.connect(audioCtx.destination);
    pair.forEach(f => {
      const osc = audioCtx.createOscillator();
      osc.frequency.value = f;
      osc.connect(gain);
      osc.start();
      osc.stop(audioCtx.currentTime + ms / 1000);
    });
  } catch { /* audio is a nicety — never let it break dialing */ }
}

// ── Device init + token refresh ───────────────────────────────────────────────
// The Voice SDK can reject or emit with undefined, so never read .message blind.
function errText(e) {
  if (!e) return 'unknown error';
  return e.message || e.description || e.causes?.[0] || String(e);
}

async function fetchToken() {
  const res = await fetch('/token');
  if (!res.ok) throw new Error('HTTP ' + res.status);
  const data = await res.json();
  if (data.error) throw new Error(data.error);
  return data;
}

function scheduleTokenRefresh(ttlSeconds) {
  clearTimeout(refreshTimer);
  tokenExpiry = Date.now() + ttlSeconds * 1000;
  setTokenExpEl.textContent = new Date(tokenExpiry).toLocaleTimeString();
  // Refresh a minute early so an in-progress call never loses its credentials.
  const delay = Math.max((ttlSeconds - 60) * 1000, 30_000);
  refreshTimer = setTimeout(refreshToken, delay);
}

async function refreshToken() {
  if (!device) return;
  try {
    const data = await fetchToken();
    device.updateToken(data.token);
    scheduleTokenRefresh(data.ttl || 3600);
  } catch (e) {
    // Retry shortly — losing the token silently kills all future calls.
    console.error('token refresh failed', e);
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refreshToken, 30_000);
  }
}

async function ensureDevice() {
  if (device) return true;
  setStatus('Initializing…');
  try {
    const data = await fetchToken();

    // Identity comes from the token response, so show it even if registration fails.
    setIdentityEl.textContent = data.identity || '—';

    device = new Device(data.token, {
      codecPreferences: ['opus', 'pcmu'],
      enableImprovedSignalingErrorPrecision: true,
    });

    device.on('error',        err => setStatus('Error: ' + errText(err), 'error'));
    device.on('unregistered', ()  => setStatus('Offline'));
    device.on('registered',   ()  => setStatus('Ready', 'ready'));
    device.on('incoming',     handleIncoming);
    device.on('tokenWillExpire', refreshToken);

    scheduleTokenRefresh(data.ttl || 3600);

    await device.register();
    refreshAudioDevices();
    setStatus('Ready', 'ready');
    return true;
  } catch (e) {
    const detail = errText(e);
    setStatus(
      /permission|NotAllowed/i.test(detail)
        ? 'Microphone blocked — allow mic access and reload'
        : 'Init failed: ' + detail,
      'error',
    );
    device = null;
    return false;
  }
}

// Register on load so inbound calls arrive without a manual first action.
// Browsers gate mic access, so this may need a click on some setups.
window.addEventListener('load', () => { ensureDevice(); });

// ── Incoming calls ────────────────────────────────────────────────────────────
function handleIncoming(call) {
  if (!setIncomingEl.checked || activeCall || auto.running) {
    call.reject();
    return;
  }
  pendingCall = call;
  const from = call.parameters?.From || 'Unknown';
  incNumberEl.textContent = formatNumber(from);
  incomingOverlay.classList.add('active');
  setStatus('Incoming call', 'calling');

  call.on('cancel',     dismissIncoming);
  call.on('disconnect', dismissIncoming);
  call.on('reject',     dismissIncoming);
}

function dismissIncoming() {
  incomingOverlay.classList.remove('active');
  pendingCall = null;
  if (!activeCall) setStatus('Ready', 'ready');
}

btnAccept.addEventListener('click', () => {
  if (!pendingCall) return;
  const call = pendingCall;
  incomingOverlay.classList.remove('active');
  pendingCall = null;
  call.accept();
  attachCall(call, call.parameters?.From || '');
});

btnReject.addEventListener('click', () => {
  pendingCall?.reject();
  dismissIncoming();
});

// ── Call handling ─────────────────────────────────────────────────────────────
btnCall.addEventListener('click', async () => {
  if (activeCall) { activeCall.disconnect(); return; }

  const raw = dialString.trim();
  if (!raw) return;

  const number = normalizeNumber(raw);
  if (!number) {
    setStatus('Invalid number — include a country code', 'error');
    return;
  }
  await placeCall(number);
});

// CRM context for the call in flight, so its outcome can be attributed.
let callCtx = { leadId: null, campaignId: null, number: '', sid: null };

async function placeCall(number, ctx = {}) {
  if (activeCall) { activeCall.disconnect(); return; }

  const ok = await ensureDevice();
  if (!ok) return;

  callCtx = {
    leadId:     ctx.leadId     ?? null,
    campaignId: ctx.campaignId ?? null,
    number,
    sid:        null,
  };

  setStatus('Calling ' + formatNumber(number) + '…', 'calling');
  btnCall.classList.add('hangup');
  btnCall.textContent = '📵';

  try {
    const call = await device.connect({ params: { To: number } });
    attachCall(call, number);
  } catch (e) {
    onCallEnded(errText(e));
  }
}

// The SDK only exposes the CallSid once signalling is under way, so this is
// retried on each call event rather than read once.
function captureSid(call) {
  if (callCtx.sid) return;
  const sid = call?.parameters?.CallSid;
  if (!sid) return;

  callCtx.sid = sid;
  if (callCtx.leadId || callCtx.campaignId) {
    api('/api/calls/link', json({
      call_sid:    sid,
      lead_id:     callCtx.leadId,
      campaign_id: callCtx.campaignId,
      to:          callCtx.number,
    })).catch(() => {});   // linkage is best-effort; never block the call
  }
}

function attachCall(call, number) {
  activeCall   = call;
  autoAnswered = false;

  btnCall.classList.add('hangup');
  btnCall.textContent = '📵';
  showInCallBar(true);

  captureSid(call);
  call.on('ringing',    () => { captureSid(call); setStatus('Ringing…', 'calling'); });
  call.on('accept',     () => {
    captureSid(call);
    autoAnswered = true;
    setStatus('Connected', 'connected');
    startCallTimer();
    showDtmfHint();
  });
  call.on('disconnect', () => onCallEnded());
  call.on('cancel',     () => onCallEnded());
  call.on('reject',     () => onCallEnded());
  call.on('error',      err => onCallEnded(errText(err)));
  call.on('mute',       isMuted => btnMute.classList.toggle('on', isMuted));
}

function onCallEnded(errMsg = null) {
  const wasAuto     = auto.running && !auto.paused;
  const wasCampaign = crm.isCampaignRunning();
  const ctx         = callCtx;
  activeCall = null;

  btnCall.classList.remove('hangup');
  btnCall.textContent = '📞';
  showInCallBar(false);
  stopCallTimer();

  if (wasAuto) {
    // index-1 is the row we just dialed.
    markDisposition(auto.index - 1, errMsg ? 'failed' : (autoAnswered ? 'answered' : 'no-answer'));
  }

  if (errMsg) {
    setStatus('Error: ' + errMsg, 'error');
    if (auto.running) stopAutoDialer('Error — stopped');
    return;
  }

  setStatus('Ready', 'ready');

  // Ask for an outcome whenever the call was attached to a lead.
  if (ctx.sid && (ctx.leadId || ctx.campaignId)) {
    crm.promptDisposition(ctx.sid, formatNumber(ctx.number));
  }

  if (wasCampaign) crm.onCampaignCallEnded();

  if (wasAuto) {
    const gapMs = Math.max(0, (parseInt(autoGapEl.value, 10) || 0)) * 1000;
    auto.timer = setTimeout(dialNext, gapMs);
  }
}

// ── In-call controls ──────────────────────────────────────────────────────────
function showInCallBar(show) {
  inCallBar.classList.toggle('active', show);
  if (!show) {
    btnMute.classList.remove('on');
    btnMute.querySelector('.ci').textContent = '🎤';
    dtmfSentEl.textContent = '';
    dtmfSentEl.classList.remove('hint');
    dtmfBuffer = '';
    setDial(dialString);   // back to previewing the number to be dialed
  }
}

function startCallTimer() {
  callStartMs = Date.now();
  clearInterval(callTimerInt);
  const tick = () => {
    const s = Math.floor((Date.now() - callStartMs) / 1000);
    const m = Math.floor(s / 60);
    callTimerEl.textContent =
      String(m).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
  };
  tick();
  callTimerInt = setInterval(tick, 1000);
}

function stopCallTimer() {
  clearInterval(callTimerInt);
  callTimerInt = null;
  callTimerEl.textContent = '00:00';
}

btnMute.addEventListener('click', () => {
  if (!activeCall) return;
  const next = !activeCall.isMuted();
  activeCall.mute(next);
  btnMute.classList.toggle('on', next);
  btnMute.querySelector('.ci').textContent = next ? '🔇' : '🎤';
});

btnHangupBar.addEventListener('click', () => activeCall?.disconnect());

function sendDtmf(digit) {
  if (!activeCall) return;
  activeCall.sendDigits(digit);
  playTone(digit);
  dtmfBuffer += digit;
  // Spaced for legibility — IVR sequences get long ("1 4 2 #").
  dtmfSentEl.textContent = 'Sent  ' + dtmfBuffer.split('').join(' ');
  dtmfSentEl.classList.remove('hint');
}

// Tell the user the keypad has changed meaning. Without this the feature is
// invisible: the dialpad silently stops editing the number and starts sending
// tones to whatever menu answered.
function showDtmfHint() {
  dtmfBuffer = '';
  dtmfSentEl.textContent = 'Keypad now sends tones — press 1, 2, ＊, ＃…';
  dtmfSentEl.classList.add('hint');
  setDial(dialString);   // repaint the line under the display for call mode
}

// ── Audio device selection ────────────────────────────────────────────────────
function refreshAudioDevices() {
  if (!device?.audio) return;

  const fill = (sel, map, current) => {
    sel.innerHTML = '';
    map.forEach((info, id) => {
      const opt = document.createElement('option');
      opt.value = id;
      opt.textContent = info.label || id;
      opt.selected = current.has?.(id);
      sel.appendChild(opt);
    });
    if (!sel.options.length) sel.innerHTML = '<option>Default</option>';
  };

  try {
    fill(setMicEl, device.audio.availableInputDevices,  new Set());
    fill(setSpkEl, device.audio.availableOutputDevices, device.audio.speakerDevices?.get() || new Set());
  } catch { /* not all browsers expose output selection */ }
}

setMicEl.addEventListener('change', () => {
  device?.audio?.setInputDevice(setMicEl.value).catch(e => setStatus('Mic error: ' + e.message, 'error'));
});

setSpkEl.addEventListener('change', () => {
  try { device?.audio?.speakerDevices.set(setSpkEl.value); }
  catch (e) { setStatus('Speaker error: ' + e.message, 'error'); }
});

btnTestAudio.addEventListener('click', () => {
  ['1', '4', '7', '*'].forEach((d, i) => setTimeout(() => playTone(d, 180), i * 220));
});

// ── Auto Dialer — input parsing ───────────────────────────────────────────────
// Split on any common separator, then normalize each entry to E.164.
// Replaces the old fixed 10-digit chunking, which broke on any non-US number.
function parseNumbers(text) {
  const parts = String(text).split(/[\n,;]+/).map(s => s.trim()).filter(Boolean);
  const valid = [];
  const invalid = [];
  const seen = new Set();
  for (const p of parts) {
    const n = normalizeNumber(p);
    if (!n) { invalid.push(p); continue; }
    if (seen.has(n)) continue;   // drop duplicates
    seen.add(n);
    valid.push(n);
  }
  return { valid, invalid };
}

function updateAutoCount() {
  const { valid, invalid } = parseNumbers(autoInput.value);
  const n = valid.length;
  let txt = n + ' number' + (n !== 1 ? 's' : '');
  if (invalid.length) txt += ` · ${invalid.length} unrecognized`;
  autoDigitCount.textContent = txt;
  autoDigitCount.className   = 'digit-count' + (n > 0 ? ' has-numbers' : '');
  return valid;
}

autoInput.addEventListener('input', updateAutoCount);

// Paste appends and re-splits, one number per line.
autoInput.addEventListener('paste', e => {
  e.preventDefault();
  const pasted = (e.clipboardData || window.clipboardData).getData('text');
  const merged = (autoInput.value ? autoInput.value + '\n' : '') + pasted;
  const { valid, invalid } = parseNumbers(merged);
  autoInput.value = valid.concat(invalid).join('\n');
  updateAutoCount();
});

// ── Auto Dialer — list rendering ──────────────────────────────────────────────
function renderAutoList() {
  autoListEl.innerHTML = auto.items.map((item, i) => `
    <div class="auto-num-item" id="auto-item-${i}">
      <span class="auto-num-text">${esc(formatNumber(item.number))}</span>
      ${item.status ? `<span class="disp ${esc(item.status)}">${esc(item.status)}</span>` : ''}
      <button class="btn-remove" data-idx="${i}" title="Remove">✕</button>
    </div>
  `).join('');

  autoListEl.querySelectorAll('.btn-remove').forEach(btn => {
    btn.addEventListener('click', () => {
      const idx = +btn.dataset.idx;
      if (idx < auto.index) return;   // already dialed — leave the record intact
      auto.items.splice(idx, 1);
      renderAutoList();
    });
  });
}

function markDisposition(idx, status) {
  if (idx < 0 || idx >= auto.items.length) return;
  auto.items[idx].status = status;
  const el = document.getElementById('auto-item-' + idx);
  if (!el) return;
  let badge = el.querySelector('.disp');
  if (!badge) {
    badge = document.createElement('span');
    el.insertBefore(badge, el.querySelector('.btn-remove'));
  }
  badge.className   = 'disp ' + status;
  badge.textContent = status;
}

function renderSummary() {
  const counts = auto.items.reduce((acc, it) => {
    if (it.status) acc[it.status] = (acc[it.status] || 0) + 1;
    return acc;
  }, {});
  const parts = Object.entries(counts).map(([k, v]) => `${k}: ${v}`);
  autoSummaryEl.textContent = parts.length ? parts.join(' · ') : '';
}

// ── Auto Dialer — start/pause/skip/stop ───────────────────────────────────────
btnStartAuto.addEventListener('click', async () => {
  const numbers = updateAutoCount();
  if (!numbers.length) { autoInput.focus(); return; }

  const ok = await ensureDevice();
  if (!ok) return;

  auto.items   = numbers.map(n => ({ number: n, status: '' }));
  auto.index   = 0;
  auto.running = true;
  auto.paused  = false;
  renderAutoList();

  autoInput.style.display      = 'none';
  autoDigitCount.style.display = 'none';
  autoListEl.style.display     = 'block';

  autoCtrlIdle.style.display    = 'none';
  autoCtrlRunning.style.display = 'block';
  btnPauseAuto.textContent      = '⏸ Pause';
  autoSummaryEl.textContent     = '';

  showView('autodialer');
  dialNext();
});

function dialNext() {
  if (!auto.running || auto.paused) return;
  if (auto.index >= auto.items.length) {
    stopAutoDialer(`✓ All ${auto.items.length} numbers dialed`);
    renderSummary();
    return;
  }

  const item = auto.items[auto.index];
  autoListEl.querySelectorAll('.auto-num-item').forEach((el, i) => {
    el.classList.toggle('done',    i < auto.index);
    el.classList.toggle('current', i === auto.index);
  });
  scrollIntoParent(document.getElementById('auto-item-' + auto.index), autoListEl);

  auto.index++;
  autoProgress.textContent =
    `Calling ${auto.index} of ${auto.items.length}: ${formatNumber(item.number)}`;
  autoProgress.className = 'live';
  renderSummary();
  placeCall(item.number);
}

function stopAutoDialer(msg = 'Stopped') {
  auto.running = false;
  auto.paused  = false;
  clearTimeout(auto.timer);

  autoCtrlRunning.style.display = 'none';
  autoCtrlIdle.style.display    = 'block';
  autoProgress.textContent      = msg;
  autoProgress.className        = '';

  // Keep the list visible so results stay readable after the run.
  renderSummary();
  setStatus('Ready', 'ready');
}

btnPauseAuto.addEventListener('click', () => {
  auto.paused = !auto.paused;
  btnPauseAuto.textContent = auto.paused ? '▶ Resume' : '⏸ Pause';
  if (auto.paused) {
    clearTimeout(auto.timer);
    autoProgress.textContent = 'Paused';
  } else if (!activeCall) {
    dialNext();
  }
});

btnSkipAuto.addEventListener('click', () => {
  if (!auto.running) return;
  clearTimeout(auto.timer);
  markDisposition(auto.index - 1, 'skipped');
  if (activeCall) {
    activeCall.disconnect();   // onCallEnded chains to the next number
  } else {
    dialNext();
  }
});

btnStopAuto.addEventListener('click', () => {
  clearTimeout(auto.timer);
  auto.running = false;        // set first so onCallEnded does not re-dial
  activeCall?.disconnect();
  stopAutoDialer('Stopped by user');
});

// Restore the input view when leaving a finished run.
btnCancelAuto.addEventListener('click', () => {
  autoInput.style.display      = '';
  autoDigitCount.style.display = '';
  autoListEl.style.display     = 'none';
  autoProgress.textContent     = '';
});

// ── Messages — thread list ────────────────────────────────────────────────────
async function loadThreads() {
  threadsListEl.innerHTML = '<p class="loading">Loading…</p>';
  try {
    const res     = await fetch('/threads');
    const threads = await res.json();
    if (threads.error) throw new Error(threads.error);
    if (!threads.length) {
      threadsListEl.innerHTML = '<p class="empty">No messages yet.</p>';
      return;
    }
    threadsListEl.innerHTML = threads.map(t => `
      <div class="thread-item" data-contact="${esc(t.contact)}">
        <div class="avatar">${esc(t.contact.replace(/\D/g,'').slice(-2))}</div>
        <div class="thread-info">
          <div class="thread-num">${esc(formatNumber(t.contact))}</div>
          <div class="thread-prev">${esc(t.body)}</div>
        </div>
        <div class="thread-date">${fmtDate(t.date)}</div>
      </div>
    `).join('');
    threadsListEl.querySelectorAll('.thread-item').forEach(el =>
      el.addEventListener('click', () => openConversation(el.dataset.contact))
    );
  } catch (e) {
    threadsListEl.innerHTML = `<p class="empty">Error: ${esc(e.message)}</p>`;
  }
}

// ── Messages — conversation ───────────────────────────────────────────────────
let currentContact = '';

async function openConversation(contact) {
  currentContact = contact;
  convContactEl.textContent = formatNumber(contact);
  convMessagesEl.innerHTML  = '<p class="loading">Loading…</p>';
  showView('conversation');
  try {
    const res  = await fetch('/messages?contact=' + encodeURIComponent(contact));
    const msgs = await res.json();
    if (msgs.error) throw new Error(msgs.error);
    renderBubbles(msgs);
  } catch (e) {
    convMessagesEl.innerHTML = `<p class="empty">Error: ${esc(e.message)}</p>`;
  }
}

function renderBubbles(msgs) {
  if (!msgs.length) { convMessagesEl.innerHTML = '<p class="empty">No messages.</p>'; return; }
  convMessagesEl.innerHTML = msgs.map(m => `
    <div class="bubble ${m.direction === 'outbound' ? 'out' : 'in'}">
      ${esc(m.body)}
      <div class="b-time">${fmtDate(m.date)}</div>
    </div>
  `).join('');
  convMessagesEl.scrollTop = convMessagesEl.scrollHeight;
}

btnSendSms.addEventListener('click', async () => {
  const body = smsBodyEl.value.trim();
  if (!body || !currentContact) return;
  smsBodyEl.value = '';
  const ok = await sendSMS(currentContact, body);
  if (ok) openConversation(currentContact);
});

// Send on Enter (Shift+Enter = newline)
smsBodyEl.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); btnSendSms.click(); }
});

btnCallContact.addEventListener('click', () => {
  if (!currentContact) return;
  setDial(currentContact);
  showView('keypad');
  placeCall(currentContact);
});

// ── Messages — compose new ────────────────────────────────────────────────────
btnSendNewSms.addEventListener('click', async () => {
  const to   = normalizeNumber(smsToEl.value);
  const body = smsTextEl.value.trim();
  if (!to)   { setStatus('Invalid number — include a country code', 'error'); return; }
  if (!body) return;

  const ok = await sendSMS(to, body);
  if (ok) {
    smsToEl.value = smsTextEl.value = '';
    showView('messages');
    loadThreads();
  }
});

async function sendSMS(to, body) {
  try {
    const res = await fetch('/send_sms', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ to, body }),
    });
    const data = await res.json();
    if (!res.ok) { setStatus('SMS failed: ' + (data.error || res.status), 'error'); return false; }
    return true;
  } catch (e) {
    setStatus('SMS error: ' + e.message, 'error');
    return false;
  }
}

// ── Recent calls ──────────────────────────────────────────────────────────────
async function loadRecent() {
  recentListEl.innerHTML = '<p class="loading">Loading…</p>';
  try {
    const res   = await fetch('/recent');
    const calls = await res.json();
    if (calls.error) throw new Error(calls.error);
    if (!calls.length) { recentListEl.innerHTML = '<p class="empty">No recent calls.</p>'; return; }
    recentListEl.innerHTML = calls.map(c => {
      const isIn   = (c.direction || '').startsWith('inbound');
      const isMiss = c.status === 'no-answer' || c.status === 'busy' || c.status === 'failed';
      const cls    = isMiss ? 'miss' : isIn ? 'in' : 'out';
      const sym    = isMiss ? '✕' : isIn ? '↙' : '↗';
      const num    = c.number || (isIn ? c.from_ : c.to);
      // Show the lead's name when we know it; the number becomes the subtitle.
      const title  = c.name || formatNumber(num) || '—';
      const sub    = c.name ? `${formatNumber(num)} · ${esc(c.status)}` : esc(c.status);
      return `
        <div class="recent-item" data-number="${esc(num)}">
          <div class="r-icon ${cls}">${sym}</div>
          <div class="r-info">
            <div class="r-num">${esc(title)}</div>
            <div class="r-meta">${sub} · ${fmtDate(c.date)}</div>
          </div>
          <div class="r-dur">${fmtDur(c.duration)}</div>
        </div>
      `;
    }).join('');
    recentListEl.querySelectorAll('.recent-item').forEach(el =>
      el.addEventListener('click', () => {
        setDial(el.dataset.number);
        showView('keypad');
      })
    );
  } catch (e) {
    recentListEl.innerHTML = `<p class="empty">Error: ${esc(e.message)}</p>`;
  }
}

// The CRM module drives calls through this interface rather than importing
// main.js, which would create a cycle.
const dialerHost = {
  placeCall,
  showView,
  setStatus,
  hangup:  () => activeCall?.disconnect(),
  isBusy:  () => !!activeCall,
};

crm.init(dialerHost);
extras.init(dialerHost);

// Drops a pre-recorded message on the leg that answered, so the agent can move
// on without waiting out the greeting.
document.getElementById('btn-vm-drop').addEventListener('click', () => {
  extras.dropVoicemail(callCtx.sid);
});

// Hooks so the call flows can be driven by tests without a live Twilio connection.
window.__crm            = crm;
window.__extras         = extras;
window.__attach         = attachCall;
window.__handleIncoming = handleIncoming;
window.__normalize      = normalizeNumber;
window.__parseNumbers   = parseNumbers;
window.__auto           = auto;
window.__renderAutoList = renderAutoList;
window.__markDisposition = markDisposition;
window.__renderSummary  = renderSummary;

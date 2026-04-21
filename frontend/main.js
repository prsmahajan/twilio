import { Device } from '@twilio/voice-sdk';

// ── DOM refs ──────────────────────────────────────────────────────────────────
const statusBar       = document.getElementById('status-bar');
const displayEl       = document.getElementById('display');
const btnBackspace    = document.getElementById('btn-backspace');
const btnCall         = document.getElementById('btn-call');
const btnAutodialer   = document.getElementById('btn-autodialer');
const btnBackAuto     = document.getElementById('btn-back-auto');
const autoInput       = document.getElementById('auto-input');
const autoDigitCount  = document.getElementById('auto-digit-count');
const autoListEl      = document.getElementById('auto-list');
const autoEmptyEl     = document.getElementById('auto-empty');
const autoProgress    = document.getElementById('auto-progress');
const btnStartAuto    = document.getElementById('btn-start-auto');
const btnCancelAuto   = document.getElementById('btn-cancel-auto');
const btnPauseAuto    = document.getElementById('btn-pause-auto');
const btnStopAuto     = document.getElementById('btn-stop-auto');
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

// ── State ─────────────────────────────────────────────────────────────────────
let device     = null;
let activeCall = null;

const auto = { queue: [], index: 0, running: false, paused: false, timer: null };

// ── Status ────────────────────────────────────────────────────────────────────
function setStatus(text, cls = '') {
  statusBar.textContent = text;
  statusBar.className   = cls;
}

// ── View navigation ───────────────────────────────────────────────────────────
const TAB_VIEWS = new Set(['keypad', 'messages', 'recent', 'settings']);
let activeView  = 'keypad';

function showView(id) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById('view-' + id).classList.add('active');
  activeView = id;
  document.querySelectorAll('.tab').forEach(t =>
    t.classList.toggle('active', t.dataset.tab === id)
  );
}

document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    const t = tab.dataset.tab;
    showView(t);
    if (t === 'messages') loadThreads();
    if (t === 'recent')   loadRecent();
  });
});

btnBackAuto.addEventListener('click',    () => showView('keypad'));
btnCancelAuto.addEventListener('click',  () => showView('keypad'));
btnBackConv.addEventListener('click',    () => { showView('messages'); loadThreads(); });
btnBackCompose.addEventListener('click', () => showView('messages'));
btnAutodialer.addEventListener('click',  () => showView('autodialer'));
btnCompose.addEventListener('click',     () => showView('compose'));

// ── Keypad input ──────────────────────────────────────────────────────────────
document.querySelectorAll('.key').forEach(key => {
  let pressTimer = null;

  key.addEventListener('mousedown', () => {
    if (key.dataset.digit === '0') {
      pressTimer = setTimeout(() => {
        pressTimer = null;
        displayEl.textContent += '+';
      }, 600);
    }
  });

  key.addEventListener('mouseup',    () => clearTimeout(pressTimer));
  key.addEventListener('mouseleave', () => clearTimeout(pressTimer));

  if (key.dataset.digit === '0') {
    key.addEventListener('contextmenu', e => {
      e.preventDefault();
      clearTimeout(pressTimer);
      pressTimer = null;
      displayEl.textContent += '+';
    });
  }

  key.addEventListener('click', () => {
    if (pressTimer === null && key.dataset.digit === '0') return; // long-press already fired
    clearTimeout(pressTimer);
    pressTimer = null;
    displayEl.textContent += key.dataset.digit;
  });
});

btnBackspace.addEventListener('click', () => {
  displayEl.textContent = displayEl.textContent.slice(0, -1);
});

// Physical keyboard input for dialer
document.addEventListener('keydown', e => {
  if (activeView !== 'keypad') return;
  const tag = document.activeElement?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA') return;
  if (/^[0-9*#+]$/.test(e.key)) {
    displayEl.textContent += e.key;
  } else if (e.key === 'Backspace') {
    displayEl.textContent = displayEl.textContent.slice(0, -1);
  } else if (e.key === 'Enter') {
    btnCall.click();
  }
});

// ── Device init (lazy — first call click is the user gesture) ─────────────────
async function ensureDevice() {
  if (device) return true;
  setStatus('Initializing…');
  try {
    const res = await fetch('/token');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    device = new Device(data.token, { codecPreferences: ['opus', 'pcmu'] });
    device.on('error',        err => setStatus('Error: ' + (err.message || err), 'error'));
    device.on('unregistered', ()  => setStatus('Idle'));

    await device.register();
    setStatus('Ready', 'ready');
    return true;
  } catch (e) {
    setStatus('Init failed: ' + e.message, 'error');
    device = null;
    return false;
  }
}

// ── Call handling ─────────────────────────────────────────────────────────────
btnCall.addEventListener('click', async () => {
  // Hangup if already in call
  if (activeCall) { activeCall.disconnect(); return; }

  const number = displayEl.textContent.trim();
  if (!number) return;
  await placeCall(number);
});

async function placeCall(number) {
  if (activeCall) { activeCall.disconnect(); return; }

  const ok = await ensureDevice();
  if (!ok) return;

  setStatus('Calling ' + number + '…', 'calling');
  btnCall.classList.add('hangup');
  btnCall.textContent = '📵';

  try {
    const call = await device.connect({ params: { To: number } });
    activeCall = call;

    call.on('ringing',    ()   => setStatus('Ringing…', 'calling'));
    call.on('accept',     ()   => setStatus('Connected', 'connected'));
    call.on('disconnect', ()   => onCallEnded());
    call.on('cancel',     ()   => onCallEnded());
    call.on('error',      err  => onCallEnded(err.message));
  } catch (e) {
    onCallEnded(e.message);
  }
}

function onCallEnded(errMsg = null) {
  const wasAuto = auto.running && !auto.paused;
  activeCall = null;

  btnCall.classList.remove('hangup');
  btnCall.textContent = '📞';

  if (errMsg) {
    setStatus('Error: ' + errMsg, 'error');
    if (auto.running) stopAutoDialer('Error — stopped');
    return;
  }

  setStatus('Ready', 'ready');

  // AutoDialer: dial next after 2s gap
  if (wasAuto) {
    auto.timer = setTimeout(dialNext, 2000);
  }
}

// ── Auto Dialer — textarea input ──────────────────────────────────────────────
let autoQueue = [];   // { number } objects used during dialing

// Parse digits from textarea into 10-digit groups, update counter
function parseAutoInput() {
  const raw    = autoInput.value.replace(/\D/g, '');
  const count  = Math.floor(raw.length / 10);
  autoDigitCount.textContent = count + ' number' + (count !== 1 ? 's' : '');
  autoDigitCount.className   = 'digit-count' + (count > 0 ? ' has-numbers' : '');
  return raw;
}

// Auto-format textarea: keep only digits, insert newline every 10
autoInput.addEventListener('input', () => {
  const sel    = autoInput.selectionStart;
  const before = autoInput.value.slice(0, sel).replace(/\D/g, '');
  const all    = autoInput.value.replace(/\D/g, '');
  // Re-format as lines of 10
  const lines  = [];
  for (let i = 0; i < all.length; i += 10) lines.push(all.slice(i, i + 10));
  autoInput.value = lines.join('\n');
  // Restore cursor roughly
  const newPos = before.length + Math.floor(before.length / 10);
  autoInput.setSelectionRange(newPos, newPos);
  parseAutoInput();
});

// Paste: strip everything non-digit, format as lines of 10
autoInput.addEventListener('paste', e => {
  e.preventDefault();
  const pasted = (e.clipboardData || window.clipboardData).getData('text');
  const all    = (autoInput.value.replace(/\D/g, '') + pasted.replace(/\D/g, ''));
  const lines  = [];
  for (let i = 0; i < all.length; i += 10) lines.push(all.slice(i, i + 10));
  autoInput.value = lines.join('\n');
  parseAutoInput();
});

// Render dial-progress list (shown while running)
function renderAutoList() {
  autoListEl.innerHTML = autoQueue.map((item, i) => `
    <div class="auto-num-item" id="auto-item-${i}">
      <span class="auto-num-text">${esc(item.number)}</span>
      <button class="btn-remove" data-idx="${i}" title="Remove">✕</button>
    </div>
  `).join('');
  autoListEl.querySelectorAll('.btn-remove').forEach(btn => {
    btn.addEventListener('click', () => {
      const idx = +btn.dataset.idx;
      if (idx < auto.index) return;   // already dialed, ignore
      autoQueue.splice(idx, 1);
      auto.queue.splice(idx, 1);
      if (idx < auto.index) auto.index--;
      renderAutoList();
    });
  });
}

// ── Auto Dialer — start/pause/stop ────────────────────────────────────────────
btnStartAuto.addEventListener('click', async () => {
  const raw = parseAutoInput();
  const nums = [];
  for (let i = 0; i + 10 <= raw.length; i += 10) nums.push(raw.slice(i, i + 10));
  if (!nums.length) { autoInput.focus(); return; }

  const ok = await ensureDevice();
  if (!ok) return;

  autoQueue = nums.map(n => ({ number: n }));
  renderAutoList();

  // Switch: hide textarea, show list
  autoInput.style.display      = 'none';
  autoDigitCount.style.display = 'none';
  autoListEl.style.display     = 'block';

  auto.queue   = nums;
  auto.index   = 0;
  auto.running = true;
  auto.paused  = false;

  autoCtrlIdle.style.display    = 'none';
  autoCtrlRunning.style.display = 'block';
  btnPauseAuto.textContent      = '⏸ Pause';

  showView('autodialer');
  dialNext();
});

function dialNext() {
  if (!auto.running || auto.paused) return;
  if (auto.index >= auto.queue.length) {
    stopAutoDialer('✓ All ' + auto.queue.length + ' numbers dialed');
    return;
  }
  const number = auto.queue[auto.index];
  // Highlight current row, dim past rows
  autoListEl.querySelectorAll('.auto-num-item').forEach((el, i) => {
    el.classList.toggle('done',    i < auto.index);
    el.classList.toggle('current', i === auto.index);
  });
  // Scroll current into view
  const cur = document.getElementById('auto-item-' + auto.index);
  cur?.scrollIntoView({ block: 'nearest' });
  auto.index++;
  autoProgress.textContent = `Calling ${auto.index} of ${auto.queue.length}: ${number}`;
  autoProgress.className   = 'live';
  placeCall(number);
}

function stopAutoDialer(msg = 'Stopped') {
  auto.running = false;
  auto.paused  = false;
  clearTimeout(auto.timer);
  autoCtrlRunning.style.display = 'none';
  autoCtrlIdle.style.display    = 'block';
  autoProgress.textContent      = msg;
  autoProgress.className        = '';
  // Restore textarea, hide list
  autoInput.style.display      = '';
  autoDigitCount.style.display = '';
  autoListEl.style.display     = 'none';
  autoQueue = [];
  setStatus('Ready', 'ready');
}

btnPauseAuto.addEventListener('click', () => {
  auto.paused = !auto.paused;
  btnPauseAuto.textContent = auto.paused ? '▶ Resume' : '⏸ Pause';
  if (!auto.paused && !activeCall) dialNext();
});

btnStopAuto.addEventListener('click', () => {
  activeCall?.disconnect();
  stopAutoDialer('Stopped by user');
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
          <div class="thread-num">${esc(t.contact)}</div>
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
async function openConversation(contact) {
  convContactEl.textContent = contact;
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
  const to   = convContactEl.textContent.trim();
  if (!body || !to) return;
  smsBodyEl.value = '';
  const ok = await sendSMS(to, body);
  if (ok) openConversation(to);
});

// Send on Enter (Shift+Enter = newline)
smsBodyEl.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); btnSendSms.click(); }
});

btnCallContact.addEventListener('click', () => {
  const number = convContactEl.textContent.trim();
  displayEl.textContent = number;
  showView('keypad');
  placeCall(number);
});

// ── Messages — compose new ────────────────────────────────────────────────────
btnSendNewSms.addEventListener('click', async () => {
  const to   = smsToEl.value.trim();
  const body = smsTextEl.value.trim();
  if (!to || !body) return;
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
      const isIn   = c.direction === 'inbound';
      const isMiss = c.status === 'no-answer' || c.status === 'busy';
      const cls    = isIn ? 'in' : isMiss ? 'miss' : 'out';
      const sym    = isIn ? '↙' : isMiss ? '↙' : '↗';
      const num    = isIn ? c.from_ : c.to;
      return `
        <div class="recent-item" data-number="${esc(num)}">
          <div class="r-icon ${cls}">${sym}</div>
          <div class="r-info">
            <div class="r-num">${esc(num || '—')}</div>
            <div class="r-meta">${esc(c.status)} · ${fmtDate(c.date)}</div>
          </div>
          <div class="r-dur">${fmtDur(c.duration)}</div>
        </div>
      `;
    }).join('');
    recentListEl.querySelectorAll('.recent-item').forEach(el =>
      el.addEventListener('click', () => {
        displayEl.textContent = el.dataset.number;
        showView('keypad');
      })
    );
  } catch (e) {
    recentListEl.innerHTML = `<p class="empty">Error: ${esc(e.message)}</p>`;
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s ?? '')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso), now = new Date();
  return d.toDateString() === now.toDateString()
    ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function fmtDur(secs) {
  const s = +secs;
  if (!s) return '—';
  return s >= 60 ? `${Math.floor(s/60)}m ${s%60}s` : `${s}s`;
}

// Follow-up tasks, call recordings, SMS templates, lead timeline, Lookup
// enrichment and calling-window checks.
//
// Kept out of crm.js for the same reason crm.js is kept out of main.js: the
// dialer core is injected through init() so no module imports its caller.

import { $, api, json, patch, esc, formatNumber, fmtDate, fmtDur } from './lib.js';
import { createCallClock } from './calltiming.js';

let dialer = null;      // { placeCall, showView, setStatus }
let config = {};        // server feature flags, see /api/config

export function init(host) {
  dialer = host;
  wireTasks();
  wireRecordings();
  wireTemplates();
  wireLeadExtras();
  wireExports();

  api('/api/config')
    .then(c => { config = c; applyConfig(); })
    .catch(() => {});
}

export const features = () => config;

// Hide controls the server cannot honour. A voicemail-drop button with no
// message configured is a button that only ever produces an error toast.
function applyConfig() {
  toggle('btn-vm-drop', config.voicemail_drop);
  toggle('recordings-row', config.recording);
  const hint = $('tpl-merge-hint');
  if (hint && config.merge_fields) {
    hint.textContent = 'Merge fields: ' +
      config.merge_fields.map(f => `{{${f}}}`).join('  ');
  }
  wireCharCounters(config.sms_max_length || 160);
}

// Every message box gets a live character budget and a hard maxlength, because
// the server refuses anything longer: past one SMS segment the carrier splits
// the text and bills per part. The template body counts raw characters — merge
// fields can render shorter, but a raw body over the limit can never be sent.
function wireCharCounters(max) {
  for (const id of ['sms-body', 'sms-text', 'tpl-body']) {
    const el = $(id);
    if (!el || el.dataset.counted) continue;
    el.dataset.counted = '1';
    el.maxLength = max;

    const out = document.createElement('div');
    out.className = 'char-count';
    out.id = `${id}-count`;
    el.insertAdjacentElement('afterend', out);

    const update = () => {
      const n = el.value.length;
      out.textContent = `${n}/${max}`;
      out.classList.toggle('near', n > max * 0.875 && n <= max);
      out.classList.toggle('over', n > max);
    };
    el.addEventListener('input', update);
    update();
  }
}

function toggle(id, on) {
  const el = $(id);
  if (el) el.style.display = on ? '' : 'none';
}

// ══════════════════════════════════════════════════════════════════════════════
// Follow-up tasks
// ══════════════════════════════════════════════════════════════════════════════

let taskScope = 'open';

export async function loadTasks() {
  const list = $('tasks-list');
  list.innerHTML = '<p class="loading">Loading…</p>';
  try {
    const tasks = await api(`/api/tasks?scope=${encodeURIComponent(taskScope)}`);
    $('tasks-count').textContent =
      tasks.length ? `${tasks.length} ${taskScope}` : '';

    list.innerHTML = tasks.length ? tasks.map(t => `
      <div class="task-row ${t.overdue ? 'overdue' : ''}" data-task="${t.id}">
        <input type="checkbox" class="task-check" data-task="${t.id}"
               ${t.done ? 'checked' : ''} />
        <div class="task-main">
          <div class="task-title">${esc(t.title)}</div>
          <div class="task-meta">
            ${t.name || t.phone ? esc(t.name || formatNumber(t.phone)) + ' · ' : ''}
            ${t.overdue ? 'overdue · ' : ''}${fmtDate(t.due_at)}
          </div>
        </div>
        ${t.phone ? `<button class="task-call" data-call="${esc(t.phone)}"
                             data-lead="${t.lead_id || ''}" title="Call">📞</button>` : ''}
        <button class="task-del" data-del="${t.id}" title="Delete">✕</button>
      </div>`).join('')
      : '<p class="empty">Nothing scheduled.</p>';
  } catch (e) {
    list.innerHTML = `<p class="empty">Error: ${esc(e.message)}</p>`;
  }
}

function wireTasks() {
  document.querySelectorAll('[data-task-scope]').forEach(btn => {
    btn.addEventListener('click', () => {
      taskScope = btn.dataset.taskScope;
      document.querySelectorAll('[data-task-scope]').forEach(b =>
        b.classList.toggle('active', b === btn));
      loadTasks();
    });
  });

  $('tasks-list').addEventListener('click', async (e) => {
    const del = e.target.closest('[data-del]');
    if (del) {
      await api(`/api/tasks/${del.dataset.del}`, { method: 'DELETE' });
      loadTasks();
      return;
    }

    const call = e.target.closest('[data-call]');
    if (call) {
      dialer.placeCall(call.dataset.call,
        call.dataset.lead ? { leadId: +call.dataset.lead } : {});
      return;
    }

    const check = e.target.closest('.task-check');
    if (check) {
      await api(`/api/tasks/${check.dataset.task}`, patch({ done: check.checked }));
      loadTasks();
    }
  });

  $('btn-task-new').addEventListener('click', () => openTaskSheet(null));
  $('btn-task-cancel').addEventListener('click', closeTaskSheet);
  $('btn-task-save').addEventListener('click', saveTask);
}

// The lead a newly created task attaches to. Null when opened from the Tasks
// tab, set when opened from a lead's detail view.
let taskLeadId = null;

export function openTaskSheet(leadId) {
  taskLeadId = leadId;
  $('task-title').value = '';
  $('task-due').value   = '+1d';
  $('task-for').textContent = leadId ? 'For the open lead' : 'Standalone reminder';
  $('task-overlay').classList.add('active');
  $('task-title').focus();
}

function closeTaskSheet() {
  $('task-overlay').classList.remove('active');
}

async function saveTask() {
  const title = $('task-title').value.trim();
  if (!title) return dialer.setStatus('Give the follow-up a title', 'error');

  try {
    await api('/api/tasks', json({
      title,
      due_at:  $('task-due').value.trim() || '+1d',
      lead_id: taskLeadId,
    }));
    closeTaskSheet();
    dialer.setStatus('Follow-up scheduled');
    if (taskLeadId) loadTimeline(taskLeadId);
    else loadTasks();
  } catch (e) {
    dialer.setStatus(e.message, 'error');
  }
}

// ══════════════════════════════════════════════════════════════════════════════
// Recordings
// ══════════════════════════════════════════════════════════════════════════════

export async function loadRecordings() {
  const list = $('recordings-list');
  list.innerHTML = '<p class="loading">Loading…</p>';
  try {
    const recs = await api('/api/recordings?limit=100');
    list.innerHTML = recs.length ? recs.map(r => `
      <div class="rec-row">
        <div class="rec-head">
          <span class="rec-who">${esc(r.name || formatNumber(r.phone) || 'Unknown')}</span>
          <span class="rec-meta">${fmtDur(r.duration)} · ${fmtDate(r.created_at)}</span>
        </div>
        <audio controls preload="none"
               src="/api/recordings/${esc(r.recording_sid)}/audio"></audio>
        <p class="rec-err" hidden></p>
        <button class="rec-del" data-rec="${esc(r.recording_sid)}">Delete</button>
      </div>`).join('')
      : '<p class="empty">No recordings yet.</p>';

    // An <audio> that 404s just renders a dead player, so the reason is read
    // back from the API and shown next to it.
    list.querySelectorAll('audio').forEach(el => {
      el.addEventListener('error', async () => {
        const note = el.parentElement.querySelector('.rec-err');
        if (!note) return;
        let msg = 'Audio unavailable.';
        try {
          const res = await fetch(el.src);
          if (!res.ok) msg = (await res.json()).error || msg;
        } catch { /* keep the generic message */ }
        note.textContent = msg;
        note.hidden = false;
      });
    });
  } catch (e) {
    list.innerHTML = `<p class="empty">Error: ${esc(e.message)}</p>`;
  }
}

function wireRecordings() {
  $('recordings-list').addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-rec]');
    if (!btn) return;
    // Deleting removes the audio from Twilio too, so it does not come back.
    if (!confirm('Delete this recording permanently?')) return;
    await api(`/api/recordings/${btn.dataset.rec}`, { method: 'DELETE' });
    loadRecordings();
  });
}

// ══════════════════════════════════════════════════════════════════════════════
// SMS templates
// ══════════════════════════════════════════════════════════════════════════════

let templates = [];

export async function loadTemplates() {
  const list = $('tpl-list');
  list.innerHTML = '<p class="loading">Loading…</p>';
  try {
    templates = await api('/api/templates');
    list.innerHTML = templates.length ? templates.map(t => `
      <div class="tpl-row" data-tpl="${t.id}">
        <div class="tpl-name">${esc(t.name)}</div>
        <div class="tpl-body">${esc(t.body)}</div>
        <button class="tpl-del" data-tpl-del="${t.id}">✕</button>
      </div>`).join('')
      : '<p class="empty">No templates yet.</p>';
    fillTemplatePicker();
  } catch (e) {
    list.innerHTML = `<p class="empty">Error: ${esc(e.message)}</p>`;
  }
}

// Populates the dropdown above the SMS composer so a template is one tap away.
function fillTemplatePicker() {
  const sel = $('sms-template');
  if (!sel) return;
  sel.innerHTML = '<option value="">Template…</option>' +
    templates.map(t => `<option value="${t.id}">${esc(t.name)}</option>`).join('');
}

function wireTemplates() {
  $('btn-tpl-save').addEventListener('click', async () => {
    const name = $('tpl-name').value.trim();
    const body = $('tpl-body').value.trim();
    if (!name || !body) return dialer.setStatus('Name and body required', 'error');
    try {
      await api('/api/templates', json({ name, body }));
      $('tpl-name').value = '';
      $('tpl-body').value = '';
      loadTemplates();
    } catch (e) {
      dialer.setStatus(e.message, 'error');
    }
  });

  $('tpl-list').addEventListener('click', async (e) => {
    const del = e.target.closest('[data-tpl-del]');
    if (!del) return;
    await api(`/api/templates/${del.dataset.tplDel}`, { method: 'DELETE' });
    loadTemplates();
  });

  const sel = $('sms-template');
  if (sel) {
    sel.addEventListener('change', () => {
      const tpl = templates.find(t => String(t.id) === sel.value);
      if (!tpl) return;
      // Merge fields are resolved server-side at send time; the composer shows
      // the raw template so it is obvious which parts are substituted.
      const box = $('sms-body') || $('sms-text');
      if (box) box.value = tpl.body;
      sel.value = '';
    });
  }

  loadTemplates().catch(() => {});
}

// ══════════════════════════════════════════════════════════════════════════════
// Lead detail — timeline, enrichment, calling window
// ══════════════════════════════════════════════════════════════════════════════

const TIMELINE_ICON = {
  call: '📞', sms: '💬', note: '📝', task: '⏰', enrich: '🔍',
  recording: '🎙️', amd: '🤖', voicemail: '📼', optout: '🚫',
  created: '✨', import: '⇪',
};

export async function loadTimeline(leadId) {
  const wrap = $('ld-timeline');
  if (!wrap) return;
  wrap.innerHTML = '<p class="loading">Loading…</p>';

  try {
    const items = await api(`/api/leads/${leadId}/timeline?limit=80`);
    wrap.innerHTML = items.length ? items.map(renderTimelineItem).join('')
      : '<p class="empty">Nothing yet.</p>';
  } catch (e) {
    wrap.innerHTML = `<p class="empty">Error: ${esc(e.message)}</p>`;
  }
}

function renderTimelineItem(it) {
  const icon = TIMELINE_ICON[it.kind] || '•';
  let body;

  if (it.kind === 'call') {
    const label = it.disposition || it.status || 'call';
    body = `<span class="disp ${esc(label)}">${esc(label)}</span>
            ${it.answered_by ? `<span class="tl-tag">${esc(it.answered_by)}</span>` : ''}
            <span class="tl-text">${esc(it.note || '')}</span>
            ${it.recording_sid
              ? `<audio controls preload="none"
                        src="/api/recordings/${esc(it.recording_sid)}/audio"></audio>`
              : ''}`;
  } else if (it.kind === 'sms') {
    body = `<span class="tl-tag">${it.direction === 'inbound' ? 'received' : 'sent'}</span>
            <span class="tl-text">${esc(it.body || '')}</span>`;
  } else {
    body = `<span class="tl-text">${esc(it.body || '')}</span>`;
  }

  return `
    <div class="tl-row">
      <span class="tl-icon">${icon}</span>
      <div class="tl-body">
        ${body}
        <div class="tl-meta">${fmtDate(it.at)}${
          it.kind === 'call' && it.duration ? ' · ' + fmtDur(it.duration) : ''}</div>
      </div>
    </div>`;
}

// The lead currently open in the detail view, tracked here so the extras panel
// can act on it without reaching into crm.js.
let openLeadId = null;

export async function showLeadExtras(lead) {
  openLeadId = lead.id;

  const badges = [];
  if (lead.line_type) badges.push(`<span class="badge">${esc(lead.line_type)}</span>`);
  if (lead.carrier)   badges.push(`<span class="badge">${esc(lead.carrier)}</span>`);
  if (lead.valid === false)
    badges.push('<span class="badge bad">not in service</span>');
  if (lead.timezone)
    badges.push(`<span class="badge">${esc(lead.timezone.split('/').pop().replace(/_/g, ' '))}</span>`);

  $('ld-badges').innerHTML = badges.join('') ||
    '<span class="muted">Not enriched yet.</span>';

  loadTimeline(lead.id);
  checkWindow(lead.phone);
  leadClock().track(lead.phone);
}

// The lead detail's own clock. Created lazily because extras.js is imported
// before the DOM is parsed, so #ld-clock does not exist at module scope.
let _leadClock = null;
function leadClock() {
  if (!_leadClock) _leadClock = createCallClock($('ld-clock'));
  return _leadClock;
}

/** Stop ticking when the detail view is left, so a closed lead costs nothing. */
export function stopLeadClock() {
  _leadClock?.stop();
}

// Advisory only — the server refuses out-of-window calls regardless. This just
// means the agent finds out before Twilio is billed for a rejected attempt.
async function checkWindow(phone) {
  const el = $('ld-window');
  if (!el) return;
  el.textContent = '';
  try {
    const c = await api(`/api/compliance/check?number=${encodeURIComponent(phone)}`);
    if (!c.enforced && !c.dnc) return;
    el.className = 'ld-window ' + (c.dialable ? 'ok' : 'bad');
    el.textContent = c.dialable
      ? `OK to call — ${c.reason}`
      : `Do not call now — ${c.reason}`;
  } catch { /* advisory only; a failure here must not block dialing */ }
}

function wireLeadExtras() {
  $('btn-ld-enrich').addEventListener('click', async () => {
    if (!openLeadId) return;
    const btn = $('btn-ld-enrich');
    btn.disabled = true;
    btn.textContent = 'Looking up…';
    try {
      const lead = await api(`/api/leads/${openLeadId}/enrich`, { method: 'POST' });
      showLeadExtras(lead);
      dialer.setStatus('Lookup complete');
    } catch (e) {
      dialer.setStatus(e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Look up carrier';
    }
  });

  $('btn-ld-task').addEventListener('click', () => openTaskSheet(openLeadId));

  $('btn-ld-note').addEventListener('click', async () => {
    const body = prompt('Add a note');
    if (!body) return;
    await api(`/api/leads/${openLeadId}/notes`, json({ body }));
    loadTimeline(openLeadId);
  });
}

// ══════════════════════════════════════════════════════════════════════════════
// Bulk enrichment and CSV export
// ══════════════════════════════════════════════════════════════════════════════

function wireExports() {
  $('btn-export-leads').addEventListener('click', () => {
    window.location.href = '/api/leads/export.csv';
  });
  $('btn-export-calls').addEventListener('click', () => {
    window.location.href = '/api/calls/export.csv';
  });

  $('btn-enrich-all').addEventListener('click', async () => {
    const btn = $('btn-enrich-all');
    // Lookup is billed per number, so the batch is capped and the cost is
    // stated before it runs rather than discovered on the invoice.
    if (!confirm('Look up carrier and line type for up to 50 leads? '
               + 'Twilio bills one Lookup request per number.')) return;
    btn.disabled = true;
    btn.textContent = 'Looking up…';
    try {
      const r = await api('/api/leads/enrich', json({ limit: 50 }));
      dialer.setStatus(
        `Enriched ${r.enriched}${r.invalid ? `, ${r.invalid} not in service` : ''}`
        + `${r.failed ? `, ${r.failed} failed` : ''}`);
    } catch (e) {
      dialer.setStatus(e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Look up carriers';
    }
  });
}

// ══════════════════════════════════════════════════════════════════════════════
// Voicemail drop
// ══════════════════════════════════════════════════════════════════════════════

export async function dropVoicemail(callSid) {
  if (!callSid) return;
  try {
    await api(`/api/calls/${callSid}/voicemail-drop`, { method: 'POST' });
    dialer.setStatus('Voicemail dropping — you can hang up');
  } catch (e) {
    dialer.setStatus(e.message, 'error');
  }
}

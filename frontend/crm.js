// Leads, import, campaigns, dispositions and analytics.
//
// The dialer core is injected via init() rather than imported, so this module
// and main.js never form an import cycle.

import { $, api, json, patch, esc, formatNumber, fmtDate, fmtDur, initials, scrollIntoParent } from './lib.js';
import { showLeadExtras, stopLeadClock } from './extras.js';
import { createCallClock } from './calltiming.js';

let dialer = null;       // { placeCall, showView, setStatus, isBusy }
let dispositionOptions = [];

export function init(host) {
  dialer = host;
  wireLeads();
  wireImport();
  wireCampaigns();
  wireDisposition();
  wireAnalytics();
  api('/api/disposition-options').then(o => { dispositionOptions = o; }).catch(() => {});
}

// ══════════════════════════════════════════════════════════════════════════════
// Leads
// ══════════════════════════════════════════════════════════════════════════════

let leadSearchTimer = null;
let currentLead = null;

export async function loadLeads(q = '') {
  const list = $('leads-list');
  list.innerHTML = '<p class="loading">Loading…</p>';
  try {
    const data = await api('/api/leads?limit=300&q=' + encodeURIComponent(q));
    $('leads-count').textContent =
      data.total + ' lead' + (data.total === 1 ? '' : 's');

    if (!data.leads.length) {
      list.innerHTML = q
        ? '<p class="empty">No leads match that search.</p>'
        : '<p class="empty">No leads yet.<br>Import a CSV or Google Sheet to get started.</p>';
      return;
    }

    list.innerHTML = data.leads.map(l => `
      <div class="lead-item" data-id="${l.id}">
        <div class="avatar">${esc(initials(l.name, l.phone))}</div>
        <div class="lead-info">
          <div class="lead-name">${esc(l.name || formatNumber(l.phone))}</div>
          <div class="lead-sub">${esc(l.company || formatNumber(l.phone))}</div>
        </div>
        ${l.dnc ? '<span class="disp dnc">DNC</span>'
                : `<span class="disp ${esc(l.status)}">${esc(l.status)}</span>`}
      </div>
    `).join('');

    list.querySelectorAll('.lead-item').forEach(el =>
      el.addEventListener('click', () => openLead(+el.dataset.id))
    );
  } catch (e) {
    list.innerHTML = `<p class="empty">Error: ${esc(e.message)}</p>`;
  }
}

async function openLead(id) {
  const data = await api('/api/leads?limit=1000');
  const lead = data.leads.find(l => l.id === id);
  if (!lead) return;
  currentLead = lead;

  $('ld-name').textContent    = lead.name || formatNumber(lead.phone);
  $('ld-phone').textContent   = formatNumber(lead.phone);
  $('ld-company').textContent = lead.company || '—';
  $('ld-email').textContent   = lead.email || '—';
  $('ld-status').value        = lead.status || 'new';
  $('ld-notes').value         = lead.notes || '';
  $('ld-dnc').checked         = lead.dnc;
  $('ld-avatar').textContent  = initials(lead.name, lead.phone);

  dialer.showView('lead-detail');

  // Timeline, enrichment badges and the calling-window notice live in extras.js.
  showLeadExtras(lead);

  const hist = $('ld-history');
  hist.innerHTML = '<p class="loading">Loading…</p>';
  try {
    const calls = await api(`/api/leads/${id}/calls`);
    hist.innerHTML = calls.length
      ? calls.map(cl => `
          <div class="hist-row">
            <span class="disp ${esc(cl.disposition || cl.status)}">${esc(cl.disposition || cl.status || '—')}</span>
            <span class="hist-note">${esc(cl.note || '')}</span>
            <span class="hist-meta">${fmtDur(cl.duration)} · ${fmtDate(cl.updated_at)}</span>
          </div>`).join('')
      : '<p class="empty">No calls yet.</p>';
  } catch {
    hist.innerHTML = '<p class="empty">Could not load history.</p>';
  }
}

function wireLeads() {
  $('leads-search').addEventListener('input', e => {
    clearTimeout(leadSearchTimer);
    leadSearchTimer = setTimeout(() => loadLeads(e.target.value.trim()), 220);
  });

  $('btn-leads-import').addEventListener('click', () => {
    $('import-result').innerHTML = '';
    dialer.showView('import');
  });

  $('btn-leads-add').addEventListener('click', () => {
    ['nl-name', 'nl-phone', 'nl-company', 'nl-email'].forEach(i => ($(i).value = ''));
    dialer.showView('lead-new');
  });

  $('btn-back-leads').addEventListener('click',  () => dialer.showView('leads'));
  $('btn-back-leads2').addEventListener('click', () => dialer.showView('leads'));
  $('btn-back-leads3').addEventListener('click', () => dialer.showView('leads'));

  $('btn-save-new-lead').addEventListener('click', async () => {
    try {
      await api('/api/leads', json({
        name:    $('nl-name').value.trim(),
        phone:   $('nl-phone').value.trim(),
        company: $('nl-company').value.trim(),
        email:   $('nl-email').value.trim(),
      }));
      dialer.showView('leads');
      loadLeads();
    } catch (e) {
      dialer.setStatus(e.message, 'error');
    }
  });

  $('btn-ld-call').addEventListener('click', () => {
    if (!currentLead) return;
    if (currentLead.dnc) {
      dialer.setStatus('Lead is marked do-not-call', 'error');
      return;
    }
    dialer.placeCall(currentLead.phone, { leadId: currentLead.id });
  });

  $('btn-ld-save').addEventListener('click', async () => {
    if (!currentLead) return;
    try {
      await api(`/api/leads/${currentLead.id}`, patch({
        status: $('ld-status').value,
        notes:  $('ld-notes').value,
        dnc:    $('ld-dnc').checked,
      }));
      dialer.showView('leads');
      loadLeads();
    } catch (e) {
      dialer.setStatus(e.message, 'error');
    }
  });

  $('btn-ld-delete').addEventListener('click', async () => {
    if (!currentLead || !confirm('Delete this lead?')) return;
    await api(`/api/leads/${currentLead.id}`, { method: 'DELETE' });
    dialer.showView('leads');
    loadLeads();
  });

  wireDeleteAll();
}

// ── Delete all leads ──────────────────────────────────────────────────────────
// Irreversible, so it sits behind its own sheet: an explicit typed confirmation
// and a do-not-call opt-out, rather than a header button one tap from Import.
function wireDeleteAll() {
  const overlay = $('wipe-overlay');
  const input   = $('wipe-confirm');
  const go      = $('btn-wipe-go');
  const keepDnc = $('wipe-keep-dnc');

  async function refreshSummary() {
    const data = await api('/api/leads?limit=1').catch(() => null);
    if (!data) return;
    const dnc = (await api('/api/leads?limit=1000').catch(() => ({ leads: [] })))
      .leads.filter(l => l.dnc).length;
    const willDelete = keepDnc.checked ? data.total - dnc : data.total;
    $('wipe-summary').textContent =
      `${willDelete} of ${data.total} lead${data.total === 1 ? '' : 's'} will be deleted` +
      (keepDnc.checked && dnc ? ` · ${dnc} do-not-call kept` : '');
    go.textContent = willDelete ? `Delete ${willDelete}` : 'Nothing to delete';
  }

  $('btn-leads-more').addEventListener('click', async () => {
    input.value = '';
    go.disabled = true;
    keepDnc.checked = true;
    await refreshSummary();
    overlay.classList.add('active');
  });

  keepDnc.addEventListener('change', refreshSummary);

  // The button only arms on an exact match.
  input.addEventListener('input', () => { go.disabled = input.value.trim() !== 'DELETE'; });

  $('btn-wipe-cancel').addEventListener('click', () => overlay.classList.remove('active'));

  go.addEventListener('click', async () => {
    go.disabled = true;
    try {
      const res = await api('/api/leads', {
        method: 'DELETE',
        body: JSON.stringify({ confirm: 'DELETE', keep_dnc: keepDnc.checked }),
      });
      overlay.classList.remove('active');
      dialer.setStatus(
        `Deleted ${res.deleted} lead${res.deleted === 1 ? '' : 's'}` +
        (res.kept_dnc ? ` · kept ${res.kept_dnc} do-not-call` : ''),
        'ready',
      );
      loadLeads();
      loadCampaigns().catch(() => {});   // campaign counts change too
    } catch (e) {
      dialer.setStatus('Delete failed: ' + e.message, 'error');
      go.disabled = false;
    }
  });
}

// ══════════════════════════════════════════════════════════════════════════════
// Import
// ══════════════════════════════════════════════════════════════════════════════

function renderImportReport(rep) {
  const bits = [`<b>${rep.imported}</b> imported`];
  if (rep.duplicates) bits.push(`${rep.duplicates} already existed`);
  if (rep.skipped)    bits.push(`${rep.skipped} unreadable`);

  $('import-result').innerHTML = `
    <div class="import-ok">${bits.join(' · ')}</div>
    ${rep.errors?.length
      ? `<div class="import-errs">${rep.errors.map(e => esc(e)).join('<br>')}</div>`
      : ''}
  `;
  loadLeads();
}

async function runImport(body, btn) {
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Importing…';
  $('import-result').innerHTML = '';
  try {
    renderImportReport(await api('/api/leads/import', body));
  } catch (e) {
    $('import-result').innerHTML = `<div class="import-err">${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

function wireImport() {
  $('btn-import-sheet').addEventListener('click', e => {
    const url = $('import-sheet-url').value.trim();
    if (!url) return;
    runImport(json({ sheet_url: url }), e.target);
  });

  $('btn-import-csv').addEventListener('click', e => {
    const text = $('import-csv').value.trim();
    if (!text) return;
    runImport(json({ csv: text }), e.target);
  });

  $('import-file').addEventListener('change', e => {
    const file = e.target.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    runImport({ method: 'POST', body: fd }, $('btn-import-csv'));
    e.target.value = '';
  });
}

// ══════════════════════════════════════════════════════════════════════════════
// Campaigns
// ══════════════════════════════════════════════════════════════════════════════

const camp = {
  id: null, name: '', queue: [], index: 0,
  running: false, paused: false, timer: null, gap: 2,
};

// Local time and pickup odds for the lead the runner is on. An unattended queue
// is exactly where a 6am dial goes unnoticed, so the campaign view carries the
// same clock the keypad does. Created lazily — #cr-clock is not in the DOM when
// this module is first imported.
let _campClock = null;
function campClock() {
  if (!_campClock) _campClock = createCallClock($('cr-clock'));
  return _campClock;
}

/** Stop the clocks belonging to views other than `id`. Called on navigation. */
export function stopClocksOutside(id) {
  if (id !== 'lead-detail')  stopLeadClock();
  if (id !== 'campaign-run') _campClock?.stop();
}

/** Point the clock at whoever is up next, for the idle and stopped states. */
function showUpcomingClock() {
  const next = camp.queue.slice(camp.index).find(q => !q.done && !q.dnc);
  campClock().track(next ? next.phone : null);
}

export async function loadCampaigns() {
  const list = $('camp-list');
  list.innerHTML = '<p class="loading">Loading…</p>';
  try {
    const cs = await api('/api/campaigns');
    if (!cs.length) {
      list.innerHTML = '<p class="empty">No campaigns yet.<br>Create one from your leads.</p>';
      return;
    }
    list.innerHTML = cs.map(c => {
      const pct = c.total ? Math.round(c.done / c.total * 100) : 0;
      return `
        <div class="camp-item" data-id="${c.id}">
          <div class="camp-top">
            <span class="camp-name">${esc(c.name)}</span>
            <span class="camp-count">${c.done}/${c.total}</span>
          </div>
          <div class="bar"><div class="bar-fill" style="width:${pct}%"></div></div>
        </div>`;
    }).join('');
    list.querySelectorAll('.camp-item').forEach(el =>
      el.addEventListener('click', () => openCampaign(+el.dataset.id))
    );
  } catch (e) {
    list.innerHTML = `<p class="empty">Error: ${esc(e.message)}</p>`;
  }
}

async function openCampaign(id) {
  const cs = await api('/api/campaigns');
  const c  = cs.find(x => x.id === id);
  if (!c) return;

  camp.id   = id;
  camp.name = c.name;
  camp.gap  = c.gap_seconds ?? 2;
  camp.running = camp.paused = false;
  clearTimeout(camp.timer);

  $('cr-name').textContent = c.name;
  await refreshQueue();
  showUpcomingClock();          // visible before Start, not only after
  dialer.showView('campaign-run');
}

async function refreshQueue() {
  camp.queue = await api(`/api/campaigns/${camp.id}/queue`);
  // Resume where the saved progress left off, so a refresh loses nothing.
  const firstOpen = camp.queue.findIndex(q => !q.done);
  camp.index = firstOpen === -1 ? camp.queue.length : firstOpen;
  renderQueue();
}

function renderQueue() {
  const done = camp.queue.filter(q => q.done).length;
  const pct  = camp.queue.length ? Math.round(done / camp.queue.length * 100) : 0;

  $('cr-progress').textContent = `${done} of ${camp.queue.length} done`;
  $('cr-bar-fill').style.width = pct + '%';

  $('cr-queue').innerHTML = camp.queue.map((q, i) => `
    <div class="auto-num-item ${q.done ? 'done' : ''} ${i === camp.index && camp.running ? 'current' : ''}">
      <span class="auto-num-text">${esc(q.name || formatNumber(q.phone))}</span>
      ${q.disposition || q.last_status
        ? `<span class="disp ${esc(q.disposition || q.last_status)}">${esc(q.disposition || q.last_status)}</span>`
        : ''}
    </div>`).join('');

  scrollIntoParent($('cr-queue').children[camp.index], $('cr-queue').parentElement);
}

function dialNextInCampaign() {
  if (!camp.running || camp.paused) return;

  while (camp.index < camp.queue.length && camp.queue[camp.index].done) camp.index++;

  if (camp.index >= camp.queue.length) {
    stopCampaign('✓ Campaign complete');
    return;
  }

  const item = camp.queue[camp.index];
  if (item.dnc) {                       // never dial a do-not-call lead
    camp.index++;
    dialNextInCampaign();
    return;
  }

  renderQueue();
  $('cr-current').textContent = item.name || formatNumber(item.phone);
  // A queue crosses timezones, so the clock has to follow the lead being dialled
  // rather than being set once when the campaign opens.
  campClock().track(item.phone);
  dialer.placeCall(item.phone, { leadId: item.lead_id, campaignId: camp.id });
}

// Called by main.js when a campaign call ends.
export function onCampaignCallEnded() {
  if (!camp.running) return;
  camp.index++;
  camp.timer = setTimeout(async () => {
    await refreshQueue().catch(() => {});
    dialNextInCampaign();
  }, Math.max(0, camp.gap) * 1000);
}

export function isCampaignRunning() {
  return camp.running;
}

function startCampaign() {
  camp.running = true;
  camp.paused  = false;
  $('cr-ctrl-idle').style.display    = 'none';
  $('cr-ctrl-running').style.display = 'block';
  dialNextInCampaign();
}

function stopCampaign(msg = 'Stopped') {
  camp.running = false;
  camp.paused  = false;
  clearTimeout(camp.timer);
  $('cr-ctrl-idle').style.display    = 'block';
  $('cr-ctrl-running').style.display = 'none';
  $('cr-current').textContent = msg;
  renderQueue();
  showUpcomingClock();
}

function wireCampaigns() {
  $('btn-camp-new').addEventListener('click', async () => {
    $('nc-name').value = '';
    const d = await api('/api/leads?limit=1');
    $('nc-lead-count').textContent = `${d.total} lead${d.total === 1 ? '' : 's'} available`;
    dialer.showView('campaign-new');
  });

  $('btn-back-camp').addEventListener('click',  () => { dialer.showView('campaigns'); loadCampaigns(); });
  $('btn-back-camp2').addEventListener('click', () => { stopCampaign(); dialer.showView('campaigns'); loadCampaigns(); });

  $('btn-create-camp').addEventListener('click', async () => {
    const name = $('nc-name').value.trim();
    if (!name) { $('nc-name').focus(); return; }
    try {
      const c = await api('/api/campaigns', json({
        name,
        all_leads:       true,
        gap_seconds:     +$('nc-gap').value || 2,
        max_attempts:    +$('nc-attempts').value || 1,
        retry_no_answer: $('nc-retry').checked,
      }));
      dialer.showView('campaigns');
      await loadCampaigns();
      openCampaign(c.id);
    } catch (e) {
      dialer.setStatus(e.message, 'error');
    }
  });

  $('btn-cr-start').addEventListener('click', startCampaign);
  $('btn-cr-stop').addEventListener('click', () => {
    camp.running = false;
    dialer.hangup();
    stopCampaign('Stopped by user');
  });

  $('btn-cr-pause').addEventListener('click', () => {
    camp.paused = !camp.paused;
    $('btn-cr-pause').textContent = camp.paused ? '▶ Resume' : '⏸ Pause';
    if (camp.paused) clearTimeout(camp.timer);
    else if (!dialer.isBusy()) dialNextInCampaign();
  });

  $('btn-cr-skip').addEventListener('click', () => {
    clearTimeout(camp.timer);
    if (dialer.isBusy()) dialer.hangup();
    else { camp.index++; dialNextInCampaign(); }
  });

  $('btn-cr-reset').addEventListener('click', async () => {
    if (!confirm('Reset all progress for this campaign?')) return;
    await api(`/api/campaigns/${camp.id}/reset`, { method: 'POST' });
    await refreshQueue();
  });

  $('btn-cr-delete').addEventListener('click', async () => {
    if (!confirm('Delete this campaign? Leads are not affected.')) return;
    await api(`/api/campaigns/${camp.id}`, { method: 'DELETE' });
    dialer.showView('campaigns');
    loadCampaigns();
  });
}

// ══════════════════════════════════════════════════════════════════════════════
// Disposition prompt
// ══════════════════════════════════════════════════════════════════════════════

let pendingSid = null;

export function promptDisposition(callSid, label) {
  if (!callSid) return;
  pendingSid = callSid;
  $('disp-for').textContent = label || '';
  $('disp-note').value = '';

  $('disp-options').innerHTML = dispositionOptions.map(d =>
    `<button class="disp-opt ${d.tone}" data-key="${d.key}">${esc(d.label)}</button>`
  ).join('');

  $('disp-options').querySelectorAll('.disp-opt').forEach(b =>
    b.addEventListener('click', () => saveDisposition(b.dataset.key))
  );

  $('disp-overlay').classList.add('active');
}

async function saveDisposition(key) {
  const sid = pendingSid;
  closeDisposition();
  try {
    await api(`/api/calls/${sid}/disposition`, json({
      disposition: key,
      note: $('disp-note').value.trim(),
    }));
  } catch (e) {
    dialer.setStatus('Could not save outcome: ' + e.message, 'error');
  }
  if (camp.running) refreshQueue().catch(() => {});
}

function closeDisposition() {
  $('disp-overlay').classList.remove('active');
  pendingSid = null;
}

function wireDisposition() {
  $('btn-disp-skip').addEventListener('click', closeDisposition);
}

// ══════════════════════════════════════════════════════════════════════════════
// Analytics
// ══════════════════════════════════════════════════════════════════════════════

// Filter state. Persisted so the range you work in survives a reload rather
// than snapping back to 14 days every time the view opens.
const AN_STORE = 'dialer.analytics.filters';
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

let anFilters = { range: '14', from: '', to: '', campaign: '' };

function loadAnFilters() {
  try {
    Object.assign(anFilters, JSON.parse(localStorage.getItem(AN_STORE) || '{}'));
  } catch { /* corrupt or unavailable storage falls back to the defaults */ }
}

function saveAnFilters() {
  try { localStorage.setItem(AN_STORE, JSON.stringify(anFilters)); } catch { }
}

// Buckets are computed in the viewer's timezone, so the offset travels with
// every request. Matches Python's expectation: minutes east of UTC.
const tzMinutes = () => -new Date().getTimezoneOffset();

function anQuery(extra = {}) {
  const p = new URLSearchParams();
  if (anFilters.range === 'custom' && anFilters.from && anFilters.to) {
    p.set('from', anFilters.from);
    p.set('to', anFilters.to);
  } else {
    p.set('days', anFilters.range === 'custom' ? '14' : anFilters.range);
  }
  if (anFilters.campaign) p.set('campaign_id', anFilters.campaign);
  p.set('tz', String(tzMinutes()));
  for (const [k, v] of Object.entries(extra)) p.set(k, v);
  return p.toString();
}

export function wireAnalytics() {
  const range = $('analytics-range');
  if (!range) return;
  loadAnFilters();

  range.value = anFilters.range;
  $('analytics-campaign').value = anFilters.campaign;
  $('analytics-from').value = anFilters.from;
  $('analytics-to').value = anFilters.to;
  syncCustomRange();

  range.addEventListener('change', () => {
    anFilters.range = range.value;
    syncCustomRange();
    saveAnFilters();
    // A freshly picked custom range has no dates yet; wait for them.
    if (anFilters.range !== 'custom' || (anFilters.from && anFilters.to)) loadAnalytics();
  });

  $('analytics-campaign').addEventListener('change', (e) => {
    anFilters.campaign = e.target.value;
    saveAnFilters();
    loadAnalytics();
  });

  for (const id of ['analytics-from', 'analytics-to']) {
    $(id).addEventListener('change', () => {
      anFilters.from = $('analytics-from').value;
      anFilters.to   = $('analytics-to').value;
      saveAnFilters();
      if (anFilters.from && anFilters.to) loadAnalytics();
    });
  }

  $('btn-analytics-export').addEventListener('click', () => {
    window.location = `/api/analytics/export.csv?${anQuery()}`;
  });

  // Twilio attaches a price to a call minutes after it ends, so the cost figures
  // fill in on demand rather than being known at hangup.
  $('btn-analytics-costs').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.textContent = 'Syncing…';
    try {
      const r = await api('/api/costs/sync', { method: 'POST' });
      const left = r.still_unpriced.calls + r.still_unpriced.messages;
      dialer.setStatus(
        `Priced ${r.calls_priced} calls, ${r.messages_priced} texts` +
        (left ? ` · ${left} still pending with Twilio` : ''));
      await loadAnalytics();
    } catch (err) {
      dialer.setStatus('Cost sync failed: ' + err.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Sync costs';
    }
  });
}

// Money is formatted to the cent for readable totals, but a sub-cent spend has
// to survive too — a 12-call day can genuinely cost $0.0042. A symbol is used
// where one is known so the figure stays on one line in a stat tile.
const CURRENCY_SYMBOLS = { USD: '$', GBP: '£', EUR: '€', INR: '₹', AUD: 'A$', CAD: 'C$' };

function fmtMoney(amount, currency) {
  const n = Number(amount) || 0;
  const digits = n > 0 && n < 0.1 ? 4 : 2;
  const symbol = CURRENCY_SYMBOLS[currency];
  return symbol ? `${symbol}${n.toFixed(digits)}`
                : `${n.toFixed(digits)} ${currency || ''}`.trim();
}

function syncCustomRange() {
  const custom = $('analytics-custom');
  if (custom) custom.hidden = anFilters.range !== 'custom';
}

// The campaign filter is populated from the campaign list rather than hardcoded,
// and refreshed on each load so a campaign created since last visit shows up.
async function fillCampaignFilter() {
  const sel = $('analytics-campaign');
  if (!sel) return;
  try {
    const campaigns = await api('/api/campaigns');
    sel.innerHTML = '<option value="">All campaigns</option>' +
      campaigns.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('');
    // A campaign that has since been deleted cannot stay selected.
    sel.value = campaigns.some(c => String(c.id) === anFilters.campaign)
      ? anFilters.campaign : '';
    if (sel.value !== anFilters.campaign) {
      anFilters.campaign = sel.value;
      saveAnFilters();
    }
  } catch { /* the filter stays at "All campaigns" */ }
}

// Period-over-period change. Prefixed with a sign, coloured by whether the
// direction is good for that metric — more calls is good, more failures is not.
function deltaChip(now, prev, { good = 'up', suffix = '' } = {}) {
  if (prev === undefined || prev === null) return '';
  if (!prev && !now) return '';
  // Up from nothing: still coloured by whether up is the good direction, so a
  // first failure does not read as a win.
  if (!prev) return `<span class="delta ${good === 'up' ? 'up' : 'down'}">new</span>`;
  const change = (now - prev) / prev * 100;
  if (Math.abs(change) < 0.5) return '<span class="delta">no change</span>';
  const dir = change > 0 ? 'up' : 'down';
  const cls = good === 'none' ? '' : (dir === good ? 'up' : 'down');
  return `<span class="delta ${cls}">${change > 0 ? '+' : ''}${change.toFixed(0)}%${suffix}</span>`;
}

function statTile(label, value, chip = '', cls = '') {
  return `<div class="stat">
    <span class="stat-n ${cls}">${value}</span>
    <span class="stat-l">${esc(label)}</span>
    ${chip}
  </div>`;
}

function barChart(rows, { value, label, title, max, cls = '' }) {
  const top = Math.max(1, max ?? Math.max(...rows.map(value)));
  return rows.map((r, i) => `
    <div class="chart-col" title="${esc(title(r, i))}">
      <div class="chart-stack">
        <div class="chart-bar ${cls}" style="height:${value(r) / top * 100}%"></div>
      </div>
      <span class="chart-lbl">${esc(label(r, i))}</span>
    </div>`).join('');
}

function breakdown(rows, key, empty, badge = true) {
  if (!rows.length) return `<p class="empty" style="padding:12px">${esc(empty)}</p>`;
  return rows.map(r => `
    <div class="brk-row">
      ${badge ? `<span class="disp ${esc(r[key])}">${esc(r[key])}</span>`
              : `<span class="brk-l">${esc(r[key])}</span>`}
      <span class="brk-n">${r.n}</span>
    </div>`).join('');
}

function funnelBlock(f) {
  const steps = [
    ['Leads',      f.leads_total, 'dim'],
    ['Dialled',    f.attempted,   ''],
    ['Reached',    f.connected,   ''],
    ['Progressed', f.advanced,    ''],
  ];
  const top = Math.max(1, f.leads_total || f.attempted);
  return steps.map(([label, n, cls]) => `
    <div class="funnel-row">
      <span class="funnel-l">${label}</span>
      <div class="funnel-track">
        <div class="funnel-fill ${cls}" style="width:${Math.min(100, (n || 0) / top * 100)}%"></div>
      </div>
      <span class="funnel-n">${n || 0}</span>
    </div>`).join('');
}

// Connect rate for every hour-and-weekday cell. Answers "when should I be
// dialling" more directly than the two separate charts, which each average over
// the other dimension.
async function heatmapSection() {
  let data;
  try {
    data = await api(`/api/analytics/timing?${anQuery()}`);
  } catch {
    return '';
  }
  const cells = new Map(data.grid.map(g => [`${g.weekday}:${g.hour}`, g]));
  if (!cells.size) return '';

  const hourLabels = ['<span></span>'].concat(
    Array.from({ length: 24 }, (_, h) =>
      `<span class="heat-lbl hour">${h % 3 ? '' : String(h).padStart(2, '0')}</span>`));

  const rows = WEEKDAYS.map((name, wd) => {
    const row = [`<span class="heat-lbl">${name}</span>`];
    for (let h = 0; h < 24; h++) {
      const cell = cells.get(`${wd}:${h}`);
      if (!cell) {
        row.push('<span class="heat-cell"></span>');
        continue;
      }
      const thin = cell.calls < data.min_attempts;
      // Rate drives opacity so the darkest cell is the best hour to call.
      const shade = thin ? '' : `background: rgba(23,23,23,${0.12 + cell.rate / 100 * 0.88})`;
      row.push(`<span class="heat-cell ${thin ? 'thin' : ''}" style="${shade}"
        title="${name} ${String(h).padStart(2, '0')}:00 — ${cell.calls} calls, ${cell.rate}% connected${thin ? ' (too few to rank)' : ''}"></span>`);
    }
    return row.join('');
  }).join('');

  return `
    <div class="sub-head">Connect rate by hour and day</div>
    <div class="heat-wrap"><div class="heat">${hourLabels.join('')}${rows}</div></div>
    <div class="chart-key">
      <span><i class="sw connected"></i>Darker is a better time to call</span>
      <span>Outlined: under ${data.min_attempts} attempts</span>
    </div>`;
}

export async function loadAnalytics() {
  const wrap = $('analytics-body');
  wrap.innerHTML = '<p class="loading">Loading…</p>';
  fillCampaignFilter();
  try {
    // compare=1 costs one extra pair of aggregate queries and gives every
    // headline number a trend, which a bare figure cannot convey.
    const a = await api(`/api/analytics?${anQuery({ compare: '1' })}`);
    const p = a.previous || {};

    // Every day in the range is drawn, including empty ones. The date labels
    // stop fitting past ten columns, so they thin out as the range widens.
    const labelEvery = a.by_day.length > 24 ? 5 : a.by_day.length > 10 ? 2 : 1;
    const dayMax = Math.max(1, ...a.by_day.map(d => d.calls));
    const days = a.by_day.map((d, i) => `
      <div class="chart-col" title="${esc(d.day)}: ${d.calls} calls, ${d.connected} connected">
        <div class="chart-stack">
          <div class="chart-bar" style="height:${d.calls / dayMax * 100}%"></div>
          <div class="chart-bar connected" style="height:${d.connected / dayMax * 100}%"></div>
        </div>
        <span class="chart-lbl">${(a.by_day.length - 1 - i) % labelEvery ? '' : esc(d.day.slice(5))}</span>
      </div>`).join('');

    // Connect rate by hour of day. Hours with no attempts are skipped rather
    // than drawn as a 0% column, which would read as "nobody answers at 3am"
    // when in fact nobody has tried.
    const activeHours = (a.by_hour || []).filter(h => h.calls > 0);
    const hours = activeHours.length ? `
      <div class="sub-head">By hour of day (your time)</div>
      <div class="chart">${barChart(activeHours, {
        value: h => h.rate,
        max: 100,
        cls: 'connected',
        label: h => String(h.hour).padStart(2, '0'),
        title: h => `${String(h.hour).padStart(2, '0')}:00 — ${h.calls} calls, ${h.rate}% connected`,
      })}</div>
      <div class="chart-key"><span><i class="sw connected"></i>Connect rate</span></div>` : '';

    const activeDows = (a.by_weekday || []).filter(d => d.calls > 0);
    const weekdays = activeDows.length ? `
      <div class="sub-head">By day of week</div>
      <div class="chart">${barChart(activeDows, {
        value: d => d.rate,
        max: 100,
        cls: 'connected',
        label: d => WEEKDAYS[d.weekday] || '?',
        title: d => `${WEEKDAYS[d.weekday]} — ${d.calls} calls, ${d.rate}% connected`,
      })}</div>
      <div class="chart-key"><span><i class="sw connected"></i>Connect rate</span></div>` : '';

    const topLeads = (a.top_leads || []).length ? `
      <div class="sub-head">Most dialled</div>
      ${a.top_leads.map(l => `
        <div class="an-lead-row">
          <span class="an-lead-who">${esc(l.name || formatNumber(l.phone) || 'Unknown')}</span>
          <span class="an-lead-meta">${l.calls}× · ${l.connected} reached · ${fmtDur(l.talk_seconds)}</span>
        </div>`).join('')}` : '';

    const sms  = a.sms || {};
    const cost = a.cost || {};
    const rangeLabel = a.days === 1 ? 'Today'
      : anFilters.range === 'custom' ? `${a.range.from} to ${a.range.to}`
      : `Last ${a.days} days`;

    wrap.innerHTML = `
      <div class="stat-grid">
        ${statTile('Calls', a.calls, deltaChip(a.calls, p.calls))}
        ${statTile('Connect rate', `${a.connect_rate}%`, deltaChip(a.connect_rate, p.connect_rate))}
        ${statTile('Talk time', fmtDur(a.talk_seconds), deltaChip(a.talk_seconds, p.talk_seconds))}
        ${statTile('Avg call', fmtDur(a.avg_duration), deltaChip(a.avg_duration, p.avg_duration))}
        ${statTile('Texts sent', sms.sent ?? 0, deltaChip(sms.sent, p.sms && p.sms.sent))}
        ${statTile('Reply rate', `${sms.reply_rate ?? 0}%`, deltaChip(sms.reply_rate, p.sms && p.sms.reply_rate))}
        ${statTile('Texts failed', sms.failed ?? 0, deltaChip(sms.failed, p.sms && p.sms.failed, { good: 'down' }))}
        ${statTile('Recordings', a.recordings ?? 0)}
        ${statTile('Spend', fmtMoney(cost.total, cost.currency),
                   deltaChip(cost.total, p.cost && p.cost.total, { good: 'down' }), 'money')}
        ${statTile('Call spend', fmtMoney(cost.calls, cost.currency), '', 'money')}
        ${statTile('Text spend', fmtMoney(cost.messages, cost.currency), '', 'money')}
        ${statTile('Cost per connect', a.connected
                   ? fmtMoney(cost.total / a.connected, cost.currency) : '—', '', 'money')}
        ${statTile('Texts delivered', sms.delivered ?? 0,
                   deltaChip(sms.delivered, p.sms && p.sms.delivered))}
        ${statTile('Leads', a.leads_total)}
        ${statTile('Do not call', a.leads_dnc)}
        ${statTile('Follow-ups', a.tasks_open ?? 0)}
        ${statTile('Overdue', a.tasks_overdue ?? 0, a.tasks_overdue ? '<span class="delta down">due</span>' : '')}
      </div>

      ${(cost.unpriced_calls || cost.unpriced_messages) ? `<div class="best-hour">
        ${cost.unpriced_calls} calls and ${cost.unpriced_messages} texts are not
        priced yet — spend is a floor, not the final bill. Tap <b>Sync costs</b>.
      </div>` : ''}

      ${a.best_hour ? `<div class="best-hour">
        Best time to call: <b>${String(a.best_hour.hour).padStart(2, '0')}:00</b> —
        ${a.best_hour.rate}% of ${a.best_hour.calls} calls connected
      </div>` : ''}

      ${a.best_weekday ? `<div class="best-hour">
        Best day: <b>${WEEKDAYS[a.best_weekday.weekday]}</b> —
        ${a.best_weekday.rate}% of ${a.best_weekday.calls} calls connected
      </div>` : ''}

      <div class="sub-head">Lead funnel</div>
      ${funnelBlock(a.funnel || {})}

      ${hours}
      ${weekdays}
      ${await heatmapSection()}

      <div class="sub-head">${esc(rangeLabel)}</div>
      ${a.calls ? `<div class="chart">${days}</div>
        <div class="chart-key">
          <span><i class="sw"></i>Calls</span>
          <span><i class="sw connected"></i>Connected</span>
        </div>` : '<p class="empty">No calls in this period.</p>'}

      <div class="sub-head">Outcomes</div>
      ${breakdown(a.by_disposition || [], 'disposition', 'No outcomes recorded yet.')}

      <div class="sub-head">Call results</div>
      ${breakdown(a.by_status || [], 'status', 'No calls in this period.')}

      <div class="sub-head">Lead stages</div>
      ${breakdown(a.by_lead_status || [], 'status', 'No leads yet.')}

      <div class="sub-head">Line types</div>
      ${breakdown(a.by_line_type || [], 'line_type', 'Run Lookup on your leads to see this.', false)}

      ${topLeads}
    `;
  } catch (e) {
    wrap.innerHTML = `<p class="empty">Error: ${esc(e.message)}</p>`;
  }
}

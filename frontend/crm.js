// Leads, import, campaigns, dispositions and analytics.
//
// The dialer core is injected via init() rather than imported, so this module
// and main.js never form an import cycle.

import { $, api, json, patch, esc, formatNumber, fmtDate, fmtDur, initials, scrollIntoParent } from './lib.js';

let dialer = null;       // { placeCall, showView, setStatus, isBusy }
let dispositionOptions = [];

export function init(host) {
  dialer = host;
  wireLeads();
  wireImport();
  wireCampaigns();
  wireDisposition();
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

export async function loadAnalytics() {
  const wrap = $('analytics-body');
  wrap.innerHTML = '<p class="loading">Loading…</p>';
  try {
    const a = await api('/api/analytics?days=14');

    const max = Math.max(1, ...a.by_day.map(d => d.calls));
    const bars = a.by_day.map(d => `
      <div class="chart-col" title="${esc(d.day)}: ${d.calls} calls, ${d.connected} connected">
        <div class="chart-stack">
          <div class="chart-bar" style="height:${d.calls / max * 100}%"></div>
          <div class="chart-bar connected" style="height:${d.connected / max * 100}%"></div>
        </div>
        <span class="chart-lbl">${esc(d.day.slice(5))}</span>
      </div>`).join('');

    const disp = a.by_disposition.length
      ? a.by_disposition.map(d => `
          <div class="brk-row">
            <span class="disp ${esc(d.disposition)}">${esc(d.disposition)}</span>
            <span class="brk-n">${d.n}</span>
          </div>`).join('')
      : '<p class="empty" style="padding:12px">No outcomes recorded yet.</p>';

    wrap.innerHTML = `
      <div class="stat-grid">
        <div class="stat"><span class="stat-n">${a.calls}</span><span class="stat-l">Calls</span></div>
        <div class="stat"><span class="stat-n">${a.connect_rate}%</span><span class="stat-l">Connect rate</span></div>
        <div class="stat"><span class="stat-n">${fmtDur(a.talk_seconds)}</span><span class="stat-l">Talk time</span></div>
        <div class="stat"><span class="stat-n">${fmtDur(a.avg_duration)}</span><span class="stat-l">Avg call</span></div>
        <div class="stat"><span class="stat-n">${a.leads_total}</span><span class="stat-l">Leads</span></div>
        <div class="stat"><span class="stat-n">${a.leads_dnc}</span><span class="stat-l">Do not call</span></div>
      </div>

      <div class="sub-head">Last ${a.days} days</div>
      ${a.by_day.length ? `<div class="chart">${bars}</div>
        <div class="chart-key">
          <span><i class="sw"></i>Calls</span>
          <span><i class="sw connected"></i>Connected</span>
        </div>` : '<p class="empty">No calls in this period.</p>'}

      <div class="sub-head">Outcomes</div>
      ${disp}
    `;
  } catch (e) {
    wrap.innerHTML = `<p class="empty">Error: ${esc(e.message)}</p>`;
  }
}

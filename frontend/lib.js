// Shared helpers used by both the dialer core and the CRM views.

export const DEFAULT_CC = '+1';

// Digits in a national number. A bare string of exactly this length is treated
// as national and gets DEFAULT_CC prepended; anything longer is assumed to
// already carry its country code. Mirrors NATIONAL_NUMBER_LENGTH in app.py.
export const NATIONAL_LEN = 10;

export function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// Mirrors normalize_number() in app.py. The '+' is optional: numbers may start
// straight with their country code (1…, 91…, 44…).
export function normalizeNumber(raw) {
  if (!raw) return null;
  raw = String(raw).trim();
  if (raw.startsWith('client:')) return raw;

  const hasPlus = raw.startsWith('+');
  const digits  = raw.replace(/\D/g, '');
  if (!digits) return null;

  let candidate;
  if (hasPlus)                              candidate = '+' + digits;
  else if (digits.length === NATIONAL_LEN)  candidate = DEFAULT_CC + digits;
  else if (digits.length > NATIONAL_LEN)    candidate = '+' + digits;
  else return null;   // too short to dial internationally

  return /^\+[1-9]\d{7,14}$/.test(candidate) ? candidate : null;
}

export function formatNumber(e164) {
  const m = /^\+1(\d{3})(\d{3})(\d{4})$/.exec(e164 || '');
  if (m) return `(${m[1]}) ${m[2]}-${m[3]}`;
  // Everything else is shown as plain E.164. Country codes are 1-3 digits and
  // cannot be split without a lookup table — guessing produced "+919 876 543"
  // for an Indian number. Unformatted is unambiguous and never wrong.
  return e164 || '';
}

export function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso.includes('T') ? iso : iso.replace(' ', 'T') + 'Z');
  if (isNaN(d)) return '';
  const now = new Date();
  return d.toDateString() === now.toDateString()
    ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

export function fmtDur(secs) {
  const s = +secs;
  if (!s) return '—';
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function initials(name, phone) {
  const n = (name || '').trim();
  if (n) {
    const parts = n.split(/\s+/);
    return ((parts[0][0] || '') + (parts[1]?.[0] || '')).toUpperCase();
  }
  return (phone || '').replace(/\D/g, '').slice(-2) || '?';
}

// Thin fetch wrapper that surfaces the server's error message instead of a bare status.
export async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body && !(opts.body instanceof FormData)
      ? { 'Content-Type': 'application/json' }
      : undefined,
    ...opts,
  });

  if (res.status === 204) return null;

  let data;
  try { data = await res.json(); } catch { data = null; }

  if (!res.ok) throw new Error(data?.error || `HTTP ${res.status}`);
  return data;
}

export const json = (obj) => ({ method: 'POST', body: JSON.stringify(obj) });
export const patch = (obj) => ({ method: 'PATCH', body: JSON.stringify(obj) });

export const $ = (id) => document.getElementById(id);

// Scroll `el` into view within `parent` ONLY.
//
// element.scrollIntoView() walks up and scrolls every scrollable ancestor. The
// inactive views sit at translateX(100%), so the browser satisfies the request
// by scrolling the .views container sideways — dragging the whole app
// off-screen. overflow:hidden does not prevent programmatic scrolling.
export function scrollIntoParent(el, parent) {
  if (!el || !parent) return;
  const top = el.offsetTop;
  const bottom = top + el.offsetHeight;
  if (top < parent.scrollTop) {
    parent.scrollTop = top;
  } else if (bottom > parent.scrollTop + parent.clientHeight) {
    parent.scrollTop = bottom - parent.clientHeight;
  }
}

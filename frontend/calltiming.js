// "Is this a good moment to ring this number?" — the live local clock where the
// callee is, plus how often that hour actually gets answered.
//
// Three views need the same answer: the keypad before a manual dial, the lead
// detail before its call button, and the campaign runner while it works a queue
// unattended. That last one matters most — nobody is watching a 200-lead blast
// discover that half the list is asleep — so the logic lives here rather than
// being retyped per view.
//
// The zone comes from the server; the ticking is local. A number therefore costs
// two requests total, not two per second.

import { esc, api } from './lib.js';

export function createCallClock(el) {
  let zone    = null;    // IANA zone the clock is counting in
  let label   = '';      // country name shown beside the time
  let outside = false;   // outside the permitted calling window
  let pickup  = '';      // rendered odds fragment, '' when unknown
  let number  = '';      // the number the current zone belongs to
  let ticker  = null;
  let pending = null;    // debounce handle

  function stop() {
    clearInterval(ticker);
    clearTimeout(pending);
    ticker = pending = null;
    zone = null;
    number = '';
    pickup = '';
    if (!el) return;
    el.hidden = true;
    el.textContent = '';
  }

  function render() {
    if (!el || !zone) return;
    let time;
    try {
      time = new Intl.DateTimeFormat([], {
        timeZone: zone, hour: '2-digit', minute: '2-digit',
        second: '2-digit', hour12: false,
      }).format(new Date());
    } catch {
      // A zone this browser's ICU data does not know: drop the clock rather
      // than showing a time from the wrong place.
      stop();
      return;
    }
    el.classList.toggle('outside', outside);
    // The country label is the first thing dropped when the warning appears:
    // all three parts together wrap onto a second line, and of the three it is
    // the one the agent can already infer from the time itself.
    el.innerHTML =
      `<span>🕐</span><span class="dc-time">${esc(time)}</span>` +
      (label && !outside ? `<span>· ${esc(label)}</span>` : '') +
      (outside ? '<span class="dc-warn">· outside hours</span>' : '') +
      pickup;
  }

  // Kept short on purpose: this shares one narrow line with the clock, and a
  // wrapped second line pushed the keypad down on every keystroke. Rates round
  // to whole percent — the decimal is false precision at n=20.
  //
  // Deliberately a separate request from the zone lookup: odds are a
  // nice-to-have and the history behind them can be empty, so a failure here
  // has to leave the clock standing.
  async function loadOdds(e164) {
    pickup = '';
    try {
      const tz   = -new Date().getTimezoneOffset();
      const odds = await api(
        `/api/analytics/pickup?number=${encodeURIComponent(e164)}&days=90&tz=${tz}`);
      if (number !== e164) return;      // number moved on while we waited

      if (odds.signal) {
        // Colour tracks the decision: green means dial now, amber means wait.
        const good = odds.rate >= odds.overall_rate;
        pickup = `<span class="dc-odds ${good ? 'good' : 'poor'}">` +
                 `· ${Math.round(odds.rate)}% answer</span>`;
      } else if (odds.best_hours.length) {
        // Too little history for *this* hour, but enough to name a better one.
        const b = odds.best_hours[0];
        pickup = `<span class="dc-odds">· best ${String(b.hour).padStart(2, '0')}:00 ` +
                 `· ${Math.round(b.rate)}%</span>`;
      }
      render();
    } catch {
      // No odds, no clock damage.
    }
  }

  async function load(e164) {
    try {
      const info = await api(
        `/api/compliance/check?number=${encodeURIComponent(e164)}`);

      number  = e164;
      zone    = info.timezone || null;
      label   = info.country || '';
      outside = info.enforced && !info.in_window;

      if (!el) return;

      if (!zone) {
        // Unknown zone: say so rather than showing the agent's own clock, which
        // would read as the callee's.
        el.hidden = false;
        el.classList.remove('outside');
        el.innerHTML =
          `<span>🌐</span><span>Local time unknown${label ? ' · ' + esc(label) : ''}</span>`;
        clearInterval(ticker);
        ticker = null;
        return;
      }

      el.hidden = false;
      render();
      clearInterval(ticker);
      ticker = setInterval(render, 1000);
      loadOdds(e164);
    } catch {
      stop();
    }
  }

  return {
    /** Point the clock at `e164`. `debounce` suits a field being typed into. */
    track(e164, { debounce = 0 } = {}) {
      if (!el) return;
      clearTimeout(pending);
      if (!e164) { stop(); return; }
      if (e164 === number) return;      // already counting for this number
      pending = debounce
        ? setTimeout(() => load(e164), debounce)
        : (load(e164), null);
    },
    stop,
  };
}

// Quote card. Degrades in place: Yahoo 429s frequently and its quoteSummary
// endpoint 401s on essentially every request, so market cap and P/E are
// routinely absent. Where we can compute an honest value we do, and we say so.

import { el, clear } from '../dom.js';
import { money, ratio, signed, timestamp, EM_DASH } from '../format.js';

function metric(label, value, delta) {
  return el('div', { class: 'metric' }, [
    el('div', { class: 'metric__label', text: label }),
    el('div', { class: 'metric__value', text: value }),
    delta || null,
  ]);
}

/** Trailing P/E from price and the XBRL diluted EPS we already retrieved. */
function derivePE(quote, financials) {
  const eps = financials && financials.EPS;
  if (!quote || !quote.price || !eps || !eps.points || !eps.points.length) return null;
  const latest = eps.points[eps.points.length - 1];
  if (!latest || !latest.value) return null;
  return { value: quote.price / latest.value, point: latest };
}

export function quoteCard(quote, financials) {
  const host = el('section', { class: 'quote', 'aria-label': 'Quote' });
  if (!quote || !quote.available) {
    host.append(el('div', { class: 'notice notice--warn' }, [
      el('b', { text: 'Quote unavailable' }),
      el('span', { text: (quote && quote.error) || 'The quote endpoint did not return a price.' }),
      el('p', { class: 'notice__fix', text: 'Every other block on this page is unaffected.' }),
    ]));
    return host;
  }

  let delta = null;
  if (quote.change !== null && quote.change !== undefined) {
    const up = quote.change >= 0;
    delta = el('div', {
      class: `metric__delta metric__delta--${up ? 'up' : 'down'}`,
      text: `${signed(quote.change)} (${signed(quote.change_percent)}%)`,
    });
  }

  const derived = quote.pe_ratio === null || quote.pe_ratio === undefined
    ? derivePE(quote, financials)
    : null;
  const peValue = derived ? derived.value : quote.pe_ratio;

  host.append(el('div', { class: 'metrics' }, [
    metric('Price', quote.price === null || quote.price === undefined ? EM_DASH : money(quote.price), delta),
    metric('Market cap', money(quote.market_cap)),
    metric('P/E', ratio(peValue)),
    metric('52-week low', money(quote.week52_low)),
    metric('52-week high', money(quote.week52_high)),
  ]));

  // Per-block provenance, always visible: this block is minutes old, while the
  // narrative below it may be months old.
  const notes = [`Quote as of ${timestamp(quote.as_of)}`];
  if (derived) {
    const cite = derived.point.citation || {};
    notes.push(
      `P/E derived from price and FY${derived.point.fiscal_year} diluted EPS`
      + (cite.form ? ` (${cite.form} filed ${cite.filed_date})` : '')
    );
  }
  if (quote.market_cap === null || quote.market_cap === undefined) {
    notes.push('market cap not returned by the quote endpoint');
  }
  if (quote.error) notes.push(quote.error);
  host.append(el('p', { class: 'provenance', text: notes.join(' · ') }));
  return host;
}

export function refreshQuoteCard(host, quote, financials) {
  clear(host).append(quoteCard(quote, financials));
}

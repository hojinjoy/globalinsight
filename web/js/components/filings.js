// Filing index. 8-K item codes identify the event before a word is read, and
// tier-3 filings are indexed by code only - their bodies are never retrieved,
// which the list must say plainly so a claim citing one reads honestly.

import { el } from '../dom.js';
import { ITEM_LABELS } from '../format.js';

export function filingIndex(eightKs, annualFiling) {
  if (!eightKs || !eightKs.length) return null;

  const list = el('ul', {});
  for (const filing of eightKs) {
    const labels = (filing.items || [])
      .map((code) => `${code} ${ITEM_LABELS[code] || 'Other'}`)
      .join(' · ');
    list.append(el('li', { 'data-accession': filing.accession }, [
      el('a', {
        href: filing.url,
        target: '_blank',
        rel: 'noopener noreferrer',
        text: filing.form,
      }),
      el('span', { class: 'filing-date', text: filing.filing_date }),
      labels ? el('span', { text: labels }) : null,
      filing.tier === 3
        ? el('span', { class: 'tag tag--indexed', text: 'indexed only' })
        : null,
    ]));
  }

  const form = (annualFiling && annualFiling.form) || 'annual report';
  const summary = el('summary', {
    text: `${eightKs.length} 8-K filings since the ${form}`,
  });
  const host = el('details', { class: 'filings' }, [summary, list]);
  host.append(el('p', { class: 'provenance', 'data-role': 'filing-provenance', text: '' }));
  return host;
}

/** Confirm which bodies were actually retrieved, once wave3_start lands. */
export function markRetrievedBodies(host, retrieved, indexedOnly) {
  if (!host) return;
  const set = new Set(retrieved || []);
  for (const item of host.querySelectorAll('li[data-accession]')) {
    const has = set.has(item.dataset.accession);
    const tag = item.querySelector('.tag--indexed');
    if (!has && !tag) {
      item.append(el('span', { class: 'tag tag--indexed', text: 'indexed only' }));
    } else if (has && tag) {
      tag.remove();
    }
  }
  const line = host.querySelector('[data-role="filing-provenance"]');
  if (line) {
    line.textContent = `${set.size} bodies retrieved, ${indexedOnly} indexed by item code`;
  }
}

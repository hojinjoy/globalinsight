// Print: popovers are worthless on paper. Every citation chip becomes a
// numbered footnote marker and the brief ends with a Sources list carrying
// form, item, date, accession, verification state and the full URL.

import { el } from './dom.js';
import { STATES, citationLabel } from './components/citation.js';

const SOURCES_ID = 'print-sources';

export function buildSources() {
  document.getElementById(SOURCES_ID)?.remove();

  const chips = [...document.querySelectorAll('.cite')];
  if (!chips.length) return;

  const seen = new Map();
  const entries = [];
  for (const chip of chips) {
    const key = `${chip.dataset.url}|${chip.querySelector('.cite__label')?.textContent || ''}`;
    if (!seen.has(key)) {
      seen.set(key, entries.length + 1);
      entries.push({
        label: chip.querySelector('.cite__label')?.textContent || '',
        url: chip.dataset.url || '',
        status: chip.dataset.status || 'cited',
      });
    }
    chip.dataset.footnote = String(seen.get(key));
  }

  const list = el('ol', {}, entries.map((entry) => el('li', {}, [
    el('span', { text: `${entry.label} — ` }),
    el('span', { text: (STATES[entry.status] || STATES.cited).label }),
    el('br'),
    el('span', { class: 'url', text: entry.url }),
  ])));

  document.querySelector('.column').append(
    el('section', { class: 'sources', id: SOURCES_ID }, [
      el('h2', { text: 'Sources' }),
      list,
    ])
  );
}

export function install() {
  window.addEventListener('beforeprint', buildSources);
  document.getElementById('print-brief').addEventListener('click', () => {
    buildSources();
    window.print();
  });
  document.addEventListener('keydown', (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'p') buildSources();
  });
}

/** Clean Markdown for a CRM note or an email. Citations inlined as links. */
export function toMarkdown(state) {
  const brief = state.brief;
  const lines = [`# ${state.ticker}${state.company ? ` — ${state.company.name}` : ''}`, ''];

  const quote = state.wave2 && state.wave2.quote;
  if (quote && quote.available) {
    lines.push(`**Price** ${quote.price} ${quote.currency || 'USD'}`
      + (quote.as_of ? `  (as of ${quote.as_of})` : ''), '');
  }

  const financials = (state.wave2 && state.wave2.financials) || {};
  const concepts = Object.values(financials);
  if (concepts.length) {
    const years = [...new Set(concepts.flatMap((c) => (c.points || []).map((p) => p.fiscal_year)))]
      .sort((a, b) => a - b).slice(-3);
    lines.push('## Financial trend', '', `| | ${years.map((y) => `FY${y}`).join(' | ')} |`,
      `| --- | ${years.map(() => '---').join(' | ')} |`);
    for (const concept of concepts) {
      const byYear = new Map((concept.points || []).map((p) => [p.fiscal_year, p.value]));
      lines.push(`| ${concept.label} | ${years.map((y) => byYear.get(y) ?? '—').join(' | ')} |`);
    }
    lines.push('', '_Retrieved from SEC XBRL structured data, never model-generated._', '');
  }

  for (const section of (brief && brief.sections) || []) {
    if (!(section.claims || []).length) continue;
    lines.push(`## ${section.heading}`, '');
    for (const claim of section.claims) {
      const cite = claim.citation || {};
      lines.push(`- ${claim.text}  \n  ↳ [${citationLabel(cite)}](${cite.url})`);
    }
    lines.push('');
  }

  if (brief && (brief.dropped_citations || []).length) {
    lines.push(`_${brief.dropped_citations.length} claim(s) withheld: cited accession(s) `
      + `${brief.dropped_citations.join(', ')} were not among the filings supplied._`, '');
  }
  lines.push('_No recommendations, price targets or buy/sell calls._');
  return lines.join('\n');
}

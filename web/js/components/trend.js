// Financial trend. These numbers are retrieved from SEC XBRL and never
// generated - the provenance line under the table is the answer to "how do we
// know it isn't making the numbers up?", so it gets visual weight.

import { el } from '../dom.js';
import { money } from '../format.js';

export function financialTrend(financials, annualFiling, errors = {}) {
  const host = el('section', { class: 'trend-block' }, [
    el('h2', { class: 'section-title', text: 'Financial trend' }),
  ]);

  if (errors.financials) {
    host.append(el('div', { class: 'notice notice--warn' }, [
      el('b', { text: 'Financial data unavailable' }),
      el('span', { text: 'The XBRL companyfacts request did not return usable data.' }),
    ]));
    return host;
  }

  const concepts = Object.values(financials || {});
  if (!concepts.length) {
    host.append(el('div', { class: 'notice' }, [
      el('span', { text: 'No XBRL annual financial data is reported for this filer.' }),
    ]));
    return host;
  }

  const years = [...new Set(concepts.flatMap((c) => (c.points || []).map((p) => p.fiscal_year)))]
    .sort((a, b) => a - b)
    .slice(-3);
  const accessions = new Set();

  const head = el('tr', {}, [el('th', { scope: 'col', text: '' })]
    .concat(years.map((year) => el('th', { scope: 'col', text: `FY${year}` }))));

  const rows = concepts.map((concept) => {
    const byYear = new Map((concept.points || []).map((p) => [p.fiscal_year, p]));
    (concept.points || []).forEach((p) => {
      if (p.citation && p.citation.accession) accessions.add(p.citation.accession);
    });
    return el('tr', {}, [el('th', { scope: 'row', text: concept.label || concept.concept })]
      .concat(years.map((year) => {
        const point = byYear.get(year);
        return el('td', { text: point ? money(point.value, concept.unit) : '—' });
      })));
  });

  host.append(el('table', { class: 'trend' }, [
    el('thead', {}, [head]),
    el('tbody', {}, rows),
  ]));

  const parts = ['Retrieved from SEC XBRL structured data, never model-generated'];
  const line = el('p', { class: 'provenance provenance--xbrl' });
  line.append(parts[0]);
  if (annualFiling && annualFiling.url) {
    line.append(' · ');
    line.append(el('a', {
      href: annualFiling.url,
      target: '_blank',
      rel: 'noopener noreferrer',
      text: `${annualFiling.form} filed ${annualFiling.filing_date}`,
    }));
  }
  if (accessions.size) line.append(` · accession ${[...accessions].sort().join(', ')}`);
  host.append(line);
  return host;
}

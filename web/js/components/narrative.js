// Wave 3: the status line, the skeletons that reserve its height, and the
// narrative itself.
//
// The skeleton exists so the page does not jump when the brief lands 45-60s
// later - the advisor may be reading the trend table at that moment.

import { el, clear } from '../dom.js';
import { count, seconds } from '../format.js';
import { citationChip } from './citation.js';
import { notice } from './notice.js';

export function waveStatus() {
  const clock = el('span', { class: 'wave-status__clock', text: '0s' });
  const label = el('span', { class: 'wave-status__label', text: 'Preparing…' });
  const host = el('div', {
    class: 'wave-status',
    role: 'status',
    'aria-live': 'polite',
  }, [label, clock]);

  return {
    node: host,
    describe(info) {
      const parts = [];
      if (info.annual && info.annual.form) {
        parts.push(`Reading ${info.annual.form} filed ${info.annual.filed}`);
      } else {
        parts.push('Reading filings');
      }
      if (info.eight_k_bodies) {
        parts.push(`+ ${info.eight_k_bodies} 8-K bodies`);
        if (info.eight_k_indexed_only) {
          parts.push(`(${info.eight_k_indexed_only} more indexed by item code)`);
        }
      }
      if (info.tokens_approx) parts.push(`— ~${count(info.tokens_approx)} tokens`);
      label.textContent = parts.join(' ');
    },
    tick(elapsed) { clock.textContent = seconds(elapsed); },
    stall(message) { label.textContent = message; clock.textContent = ''; },
    remove() { host.remove(); },
  };
}

export function skeleton(rows = 7) {
  const bars = [];
  // Widths approximate the finished brief so the reserved height is honest.
  const widths = ['62%', '96%', '88%', '91%', '40%', '94%', '86%', '89%', '45%', '92%'];
  for (let index = 0; index < rows; index += 1) {
    bars.push(el('div', { class: 'skeleton__bar', style: `width:${widths[index % widths.length]}` }));
  }
  return el('div', { class: 'skeleton', 'aria-hidden': 'true' }, bars);
}

function claimItem(claim, lead = false) {
  const chip = citationChip(claim.citation, { quote: claim.quote, status: claim.status });
  // A claim with no citation does not render. That constraint is the product.
  if (!chip) return null;
  return el('li', { class: 'claim' }, [
    el('p', { class: `claim__text${lead ? ' claim__text--lead' : ''}`, text: claim.text }),
    chip,
  ]);
}

export function narrative(brief) {
  const host = el('div', { class: 'narrative' });
  for (const section of brief.sections || []) {
    const claims = (section.claims || [])
      .map((claim, index) => claimItem(claim, section.key === 'business_line' && index === 0))
      .filter(Boolean);
    if (!claims.length) continue;
    host.append(el('h3', { text: section.heading }));
    host.append(el('ul', { class: 'claims' }, claims));
  }

  if (!host.children.length) {
    host.append(notice({
      tone: 'warn',
      title: 'No citable claims survived',
      message: 'Every bullet the model produced cited a filing that was not supplied, so none were rendered.',
    }));
  }

  // Show the catch rather than hiding it: this is the strongest live evidence
  // of the compliance story, and in the normal case it renders nothing.
  const dropped = brief.dropped_citations || [];
  if (dropped.length) {
    host.append(notice({
      tone: 'info',
      title: `${dropped.length} claim${dropped.length === 1 ? '' : 's'} withheld.`,
      message: `${dropped.length === 1 ? 'It cited accession' : 'They cited accessions'} `
        + `${dropped.join(', ')}, which ${dropped.length === 1 ? 'was' : 'were'} not among the `
        + 'filings supplied to the model. Withheld rather than shown as sourced.',
    }));
  }
  return host;
}

export function narrativeProvenance(annualFiling, eightKs, elapsed) {
  const parts = [];
  if (annualFiling) parts.push(`Grounded in ${annualFiling.form} filed ${annualFiling.filing_date}`);
  else parts.push('Grounded in the filings retrieved above');
  if (eightKs && eightKs.length) {
    parts.push(`+ ${eightKs.length} 8-Ks through ${eightKs[0].filing_date}`);
  }
  if (elapsed !== null && elapsed !== undefined) parts.push(`synthesised in ${seconds(elapsed)}`);
  return el('p', { class: 'provenance', text: parts.join(' · ') });
}

export function replaceWith(host, node) {
  clear(host).append(node);
}

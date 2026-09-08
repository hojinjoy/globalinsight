// Citation chip + popover. The load-bearing component: this is where the
// product's compliance claim is either made visible or quietly undermined.
//
// Four states, and we never render a check mark we have not earned. A brief
// claim carries no verbatim excerpt (the backend's citation schema has no
// `quote` field), so it shows as neutral "Cited", not Verified.

import { el } from '../dom.js';

export const STATES = {
  verified: { glyph: '✓', label: 'Quote located in source', className: 'cite--verified' },
  unverified: { glyph: '!', label: 'Quote not located — verify before use', className: 'cite--unverified' },
  metadata: { glyph: '◦', label: 'Filing indexed by item code; body not retrieved', className: 'cite--metadata' },
  cited: { glyph: '→', label: 'Cited to filing; excerpt not captured', className: 'cite--cited' },
};

export function citationLabel(citation) {
  if (!citation) return '';
  // A 10-K has no item number by design (supplied whole-document), so no slot,
  // placeholder or dash is rendered for it.
  const item = citation.item ? `, Item ${citation.item}` : '';
  return `${citation.form}${item}, filed ${citation.filed_date}`;
}

let openPopover = null;

export function closePopover({ restoreFocus = true } = {}) {
  if (!openPopover) return;
  const { node, trigger } = openPopover;
  node.remove();
  trigger.setAttribute('aria-expanded', 'false');
  openPopover = null;
  if (restoreFocus) trigger.focus();
}

function buildPopover(citation, quote, state) {
  const body = [
    el('p', { class: 'popover__state', text: `${state.glyph} ${state.label}` }),
  ];
  if (quote) body.push(el('blockquote', { class: 'popover__quote', text: quote }));
  else body.push(el('p', { class: 'popover__none', text: 'No excerpt was captured for this claim.' }));
  body.push(el('p', { class: 'popover__meta', text: `Accession ${citation.accession || 'unknown'}` }));
  body.push(el('a', {
    href: citation.url,
    target: '_blank',
    rel: 'noopener noreferrer',
    text: 'Open filing on EDGAR ↗',
  }));
  return el('div', { class: 'popover', role: 'dialog', 'aria-label': 'Citation detail' }, body);
}

export function citationChip(citation, { quote = '', status = 'cited' } = {}) {
  if (!citation) return null;
  const state = STATES[status] || STATES.cited;
  const chip = el('button', {
    type: 'button',
    class: `cite ${state.className}`,
    'aria-expanded': 'false',
    'aria-label': `${citationLabel(citation)}. ${state.label}.`,
    'data-status': status,
    'data-url': citation.url || '',
  }, [
    el('span', { class: 'cite__glyph', 'aria-hidden': 'true', text: state.glyph }),
    el('span', { class: 'cite__label', text: citationLabel(citation) }),
  ]);

  chip.addEventListener('click', () => {
    const wasOpen = openPopover && openPopover.trigger === chip;
    closePopover({ restoreFocus: false });
    if (wasOpen) return;
    const node = buildPopover(citation, quote, state);
    document.getElementById('popover-root').append(node);
    const box = chip.getBoundingClientRect();
    node.style.top = `${window.scrollY + box.bottom + 6}px`;
    node.style.left = `${Math.max(8, Math.min(window.scrollX + box.left, window.innerWidth - node.offsetWidth - 12))}px`;
    chip.setAttribute('aria-expanded', 'true');
    openPopover = { node, trigger: chip };
  });

  // Arrow keys move between chips within the same claim list.
  chip.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
    const chips = Array.from(chip.closest('.narrative, .answer, .thread')?.querySelectorAll('.cite') || []);
    const index = chips.indexOf(chip);
    const next = chips[index + (event.key === 'ArrowRight' ? 1 : -1)];
    if (next) { event.preventDefault(); next.focus(); }
  });

  return chip;
}

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closePopover();
});
document.addEventListener('click', (event) => {
  if (!openPopover) return;
  if (openPopover.node.contains(event.target) || openPopover.trigger.contains(event.target)) return;
  closePopover({ restoreFocus: false });
});

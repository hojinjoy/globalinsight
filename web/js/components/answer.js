// Follow-up answer. Text streams in as it forms; claims and the usage footer
// land when the structured payload arrives.

import { el, clear } from '../dom.js';
import { cost, count, seconds } from '../format.js';
import { citationChip } from './citation.js';
import { notice } from './notice.js';

export function answerCard() {
  const text = el('div', { class: 'answer__text' });
  const caret = el('span', { class: 'answer__caret', 'aria-hidden': 'true' });
  const body = el('p', {});
  text.append(body, caret);
  const claims = el('ul', { class: 'claims' });
  const usage = el('p', { class: 'answer__usage' });
  const host = el('section', { class: 'answer', 'aria-live': 'polite' }, [text, claims, usage]);

  let buffer = '';
  return {
    node: host,
    delta(chunk) {
      buffer += chunk;
      body.textContent = buffer;
    },
    finish(answer) {
      caret.remove();
      clear(text);
      for (const paragraph of (answer.text || buffer).split(/\n{2,}/)) {
        if (paragraph.trim()) text.append(el('p', { text: paragraph.trim() }));
      }
      if (answer.unsupported) {
        text.append(notice({ tone: 'info', message: answer.unsupported }));
      }
      clear(claims);
      for (const claim of answer.claims || []) {
        const chip = citationChip(claim.citation, { quote: claim.quote, status: claim.status });
        if (!chip) continue;
        claims.append(el('li', { class: 'claim' }, [
          el('p', { class: 'claim__text', text: claim.text }),
          chip,
        ]));
      }
      const use = answer.usage || {};
      const parts = [seconds(use.seconds), cost(use.cost)];
      if (use.cache_read) parts.push(`${count(use.cache_read)} tokens read from cache`);
      else if (use.cache_write) parts.push(`${count(use.cache_write)} tokens written to cache`);
      if (use.checkable) parts.push(`${use.verified}/${use.checkable} quotes verified`);
      if (use.indexed_only) parts.push(`${use.indexed_only} cited by item code only`);
      usage.textContent = parts.join(' · ');
    },
    fail(node) {
      caret.remove();
      clear(text).append(node);
    },
  };
}

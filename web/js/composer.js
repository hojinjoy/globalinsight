// Composer: ticker, company name, or a question. Typeahead is server-side
// because the ticker table is 10,415 rows and already cached in Python.

import { el, clear } from './dom.js';
import { tickers } from './api.js';

export function composer({ onSubmit }) {
  const form = document.getElementById('composer');
  const input = document.getElementById('prompt');
  const list = document.getElementById('typeahead');
  const error = document.getElementById('composer-error');

  let items = [];
  let active = -1;
  let debounce = null;

  const hide = () => {
    list.hidden = true;
    clear(list);
    items = [];
    active = -1;
    input.setAttribute('aria-expanded', 'false');
  };

  const choose = (index) => {
    const item = items[index];
    if (!item) return;
    input.value = item.ticker;
    hide();
    form.requestSubmit();
  };

  const highlight = (index) => {
    active = index;
    [...list.children].forEach((node, position) => {
      node.setAttribute('aria-selected', position === index ? 'true' : 'false');
    });
  };

  const render = (rows) => {
    items = rows;
    clear(list);
    if (!rows.length) return hide();
    rows.forEach((row, index) => {
      list.append(el('li', {
        role: 'option',
        id: `typeahead-${index}`,
        'aria-selected': 'false',
        onmousedown: (event) => { event.preventDefault(); choose(index); },
      }, [el('b', { text: row.ticker }), el('span', { text: row.name })]));
    });
    list.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    highlight(-1);
  };

  input.addEventListener('input', () => {
    error.hidden = true;
    const value = input.value.trim();
    clearTimeout(debounce);
    // Only symbol-ish fragments get a typeahead; a sentence is a question.
    if (value.length < 2 || value.includes(' ')) return hide();
    debounce = setTimeout(async () => {
      try {
        render(await tickers(value.replace(/^\$/, '')));
      } catch {
        hide();
      }
    }, 90);
  });

  input.addEventListener('keydown', (event) => {
    if (list.hidden) return;
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      highlight(Math.min(active + 1, items.length - 1));
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      highlight(Math.max(active - 1, 0));
    } else if (event.key === 'Enter' && active >= 0) {
      event.preventDefault();
      choose(active);
    } else if (event.key === 'Escape') {
      hide();
    }
  });

  input.addEventListener('blur', () => setTimeout(hide, 120));

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const value = input.value.trim();
    if (!value) return;
    hide();
    input.value = '';
    onSubmit(value);
  });

  // "/" focuses the composer from anywhere.
  document.addEventListener('keydown', (event) => {
    if (event.key !== '/' || event.target === input) return;
    if (['INPUT', 'TEXTAREA'].includes(event.target.tagName)) return;
    event.preventDefault();
    input.focus();
  });

  return {
    focus: () => input.focus(),
    setValue: (value) => { input.value = value; },
    showError: (message) => {
      error.textContent = message;
      error.hidden = false;
    },
  };
}

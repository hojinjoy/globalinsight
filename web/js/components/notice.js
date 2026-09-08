import { el } from '../dom.js';

// An `unavailable` state (an ETF has no filings) must not look like an error.
export function notice({ tone = 'info', title = '', message = '', actionable = '' }) {
  return el('div', { class: `notice notice--${tone}`, role: tone === 'error' ? 'alert' : 'status' }, [
    title ? el('b', { text: title }) : null,
    el('span', { text: message }),
    actionable ? el('p', { class: 'notice__fix', text: actionable }) : null,
  ]);
}

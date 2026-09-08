// Environment strip. Turns red before the user types if SEC_USER_AGENT is
// rejected - a 403 discovered mid-demo is worse than one flagged up front.

import { el, clear } from '../dom.js';
import { bytes } from '../format.js';

function row(tone, glyph, label, detail, fix) {
  return el('div', { class: `env__row env--${tone}` }, [
    el('span', { class: 'env__dot', 'aria-hidden': 'true', text: glyph }),
    el('span', {}, [
      el('b', { text: label }),
      detail ? el('span', { text: ` ${detail}` }) : null,
      fix ? el('p', { class: 'env__fix', text: fix }) : null,
    ]),
  ]);
}

export function environmentStrip(host, health) {
  clear(host);
  if (!health) {
    host.append(row('warn', '·', 'Environment', 'unavailable'));
    return;
  }

  if (health.mode === 'replay') {
    host.append(el('div', {}, [el('span', { class: 'badge-replay', text: 'REPLAY' })]));
  }

  host.append(health.sec_user_agent_configured
    ? row('ok', '✓', 'SEC User-Agent', 'set')
    : row('bad', '✕', 'SEC User-Agent', 'rejected',
        'Set SEC_USER_AGENT to a real contact address. The SEC returns 403 for '
        + 'generic browser strings and any address at example.com.'));

  host.append(health.credentials
    ? row('ok', '✓', 'Claude credentials', 'found')
    : row('warn', '!', 'Claude credentials', 'missing',
        'Quote, financials and the filing index still work. The narrative does not.'));

  host.append(row('ok', '·', 'Cache', bytes(health.cache_bytes)));
  if (health.mode === 'replay' && (health.fixtures_available || []).length) {
    host.append(row('ok', '·', 'Recorded', health.fixtures_available.join(', ')));
  }
}

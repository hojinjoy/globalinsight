// Orchestration: routing, session state, and the three-wave render.
//
// Prior turns render from a client-side cache, so turn 6 costs the same as
// turn 1 - unlike the Streamlit prototype, which re-ran the whole script and
// re-rendered every prior turn on each message.

import { el, clear } from './dom.js';
import * as api from './api.js';
import { composer } from './composer.js';
import { environmentStrip } from './components/environment.js';
import { quoteCard } from './components/quote.js';
import { financialTrend } from './components/trend.js';
import { filingIndex, markRetrievedBodies } from './components/filings.js';
import {
  narrative, narrativeProvenance, skeleton, waveStatus,
} from './components/narrative.js';
import { answerCard } from './components/answer.js';
import { notice } from './components/notice.js';
import { closePopover } from './components/citation.js';
import * as printer from './print.js';

const DEMO_TICKERS = ['NVDA', 'AAPL', 'MSFT', 'JPM'];

const thread = document.getElementById('thread');
const welcome = document.getElementById('welcome');
const main = document.getElementById('main');
const sessionList = document.getElementById('session-list');
const sessionTitle = document.getElementById('session-title');

const state = {
  health: null,
  active: null,
  briefs: new Map(),   // ticker -> rendered state, so the rail re-renders free
  histories: new Map(), // ticker -> prior Q&A turns
  busy: false,
};

let input;

// --- session rail ------------------------------------------------------------

function renderRail() {
  clear(sessionList);
  sessionTitle.hidden = state.briefs.size === 0;
  for (const [ticker, entry] of state.briefs) {
    sessionList.append(el('li', {}, [
      el('button', {
        type: 'button',
        'aria-current': ticker === state.active ? 'true' : 'false',
        onclick: () => showTicker(ticker),
      }, [
        el('span', { text: ticker }),
        el('small', { text: (entry.company && entry.company.name) || '' }),
      ]),
    ]));
  }
}

function scrollToEnd() {
  main.scrollTop = main.scrollHeight;
}

function addUserTurn(text) {
  welcome.hidden = true;
  thread.append(el('div', { class: 'turn turn--user', text }));
  scrollToEnd();
}

// --- brief -------------------------------------------------------------------

function briefShell(ticker) {
  const head = el('div', { class: 'brief__head' }, [
    el('h2', { class: 'brief__ticker', tabindex: '-1', text: ticker }),
    el('span', { class: 'brief__name' }),
    el('span', { class: 'form-badge', hidden: true }),
  ]);
  const noteHost = el('div', { class: 'brief__note' });
  const quoteHost = el('div', { class: 'brief__quote' });
  const dataHost = el('div', { class: 'brief__data' });
  const wave3Host = el('div', { class: 'brief__wave3' });
  const card = el('div', { class: 'card' }, [head, noteHost, quoteHost, dataHost, wave3Host]);
  const turn = el('div', { class: 'turn turn--brief' }, [card]);
  thread.append(turn);
  return { turn, head, noteHost, quoteHost, dataHost, wave3Host };
}

async function runBrief(ticker) {
  const nodes = briefShell(ticker);
  const entry = { ticker, company: null, wave2: null, brief: null };
  state.briefs.set(ticker, entry);
  state.active = ticker;
  window.history.replaceState(null, '', `#/${ticker}`);
  renderRail();
  scrollToEnd();

  const status = waveStatus();
  let bones = null;
  let filings = null;
  let settled = false;

  const handlers = {
    // Replayed data is never presented as live: the fixture's own note says
    // what it is, alongside the REPLAY badge in the environment strip.
    meta(payload) {
      if (payload.mode === 'replay' && payload.replay_note) {
        nodes.noteHost.append(notice({
          tone: 'warn', title: 'Replay', message: payload.replay_note,
        }));
      }
    },

    wave2(payload) {
      entry.wave2 = payload;
      entry.company = payload.company;
      if (payload.company) {
        nodes.head.querySelector('.brief__name').textContent = payload.company.name;
      }
      const annual = payload.annual_filing;
      if (annual && annual.form) {
        const badge = nodes.head.querySelector('.form-badge');
        badge.textContent = annual.form;
        badge.hidden = false;
      }

      clear(nodes.quoteHost).append(quoteCard(payload.quote, payload.financials));
      clear(nodes.dataHost);
      const noFilingsExpected = payload.classification === 'etf'
        || payload.classification === 'unknown';
      if (!noFilingsExpected || Object.keys(payload.financials || {}).length) {
        nodes.dataHost.append(financialTrend(payload.financials, annual, payload.errors));
      }
      filings = filingIndex(payload.eight_ks, annual);
      if (filings) nodes.dataHost.append(filings);

      // Structurally absent is not an error, and must not look like one.
      if (payload.classification === 'etf') {
        nodes.dataHost.append(notice({
          tone: 'info',
          title: 'No corporate filings',
          message: 'ETFs and funds file no annual report, so the filing half of this '
            + 'brief is empty by design.',
        }));
      } else if (payload.classification === 'unknown') {
        // Not a ticker at all. Say so plainly rather than reserving a skeleton
        // for a wave 3 that will never arrive.
        nodes.dataHost.append(notice({
          tone: 'warn',
          title: `"${payload.ticker}" is not a recognised ticker`,
          message: 'No SEC-registered filer and no market quote matched this symbol. '
            + 'Check the spelling, or try the company name instead.',
        }));
      } else if (payload.classification === 'pre_annual') {
        const forms = [...new Set((payload.other_filings || []).map((f) => f.form))].join(', ');
        nodes.dataHost.append(notice({
          tone: 'info',
          title: 'No annual report on file yet',
          message: `Only ${forms || 'pre-annual filings'} are on file. A recent IPO has `
            + 'nothing to summarise until its first 10-K.',
        }));
      } else {
        // Reserve wave 3's height now, so nothing moves when it lands.
        bones = skeleton();
        nodes.wave3Host.append(status.node, bones);
      }
      scrollToEnd();
    },

    wave3_start(payload) {
      status.describe(payload);
      markRetrievedBodies(filings, payload.bodies_retrieved, payload.eight_k_indexed_only);
    },

    progress(payload) {
      status.tick(payload.elapsed_seconds);
    },

    brief(payload) {
      settled = true;
      entry.brief = payload;
      status.remove();
      if (bones) bones.remove();
      nodes.wave3Host.append(narrative(payload));
      const annual = entry.wave2 && entry.wave2.annual_filing;
      const eightKs = (entry.wave2 && entry.wave2.eight_ks) || [];
      nodes.wave3Host.append(narrativeProvenance(annual, eightKs, entry.elapsed));
      document.getElementById('copy-brief').disabled = false;
      document.getElementById('print-brief').disabled = false;
      // Land keyboard users on the new content, not back in the composer.
      nodes.head.querySelector('.brief__ticker').focus();
      scrollToEnd();
    },

    block_error(payload) {
      settled = true;
      status.remove();
      if (bones) bones.remove();
      const tone = payload.kind === 'no_credits' ? 'warn' : 'error';
      nodes.wave3Host.append(notice({
        tone,
        title: payload.kind === 'no_credits' ? 'Narrative unavailable' : 'Something did not load',
        message: payload.message,
        actionable: payload.actionable,
      }));
      scrollToEnd();
    },

    done(payload) {
      entry.elapsed = payload.elapsed_seconds;
      if (!settled) {
        status.remove();
        if (bones) bones.remove();
      }
      scrollToEnd();
    },

    streamerror() {
      status.stall('Connection lost.');
      if (bones) bones.remove();
      const retry = el('button', {
        type: 'button',
        class: 'btn',
        text: 'Retry',
        onclick: () => { nodes.turn.remove(); runBrief(ticker); },
      });
      nodes.wave3Host.append(notice({
        tone: 'error',
        title: 'Connection lost',
        message: 'Everything already on screen above is unaffected.',
      }), retry);
    },
  };

  const stream = api.briefStream(ticker, handlers);
  await stream.done;
}

/** Re-render a ticker already in this session. No network. */
function showTicker(ticker) {
  const entry = state.briefs.get(ticker);
  if (!entry) return;
  state.active = ticker;
  window.history.replaceState(null, '', `#/${ticker}`);
  renderRail();
  const heading = [...thread.querySelectorAll('.brief__ticker')]
    .find((node) => node.textContent === ticker);
  if (heading) heading.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// --- follow-up ---------------------------------------------------------------

async function runAsk(ticker, question) {
  const card = answerCard();
  thread.append(el('div', { class: 'turn turn--answer' }, [el('div', { class: 'card' }, [card.node])]));
  scrollToEnd();

  const history = state.histories.get(ticker) || [];
  const handlers = {
    delta(payload) { card.delta(payload.text); scrollToEnd(); },
    answer(payload) {
      card.finish(payload);
      history.push({ role: 'user', content: question });
      history.push({ role: 'assistant', content: payload.text });
      state.histories.set(ticker, history);
      scrollToEnd();
    },
    block_error(payload) {
      card.fail(notice({
        tone: payload.kind === 'no_credits' ? 'warn' : 'error',
        title: 'No answer',
        message: payload.message,
        actionable: payload.actionable,
      }));
    },
    streamerror() {
      card.fail(notice({ tone: 'error', title: 'Connection lost', message: 'The answer did not complete.' }));
    },
    done() { scrollToEnd(); },
  };

  const stream = api.askStream({ ticker, question, history }, handlers);
  await stream.done;
}

// --- submit ------------------------------------------------------------------

async function submit(text) {
  if (state.busy) return;
  state.busy = true;
  closePopover({ restoreFocus: false });
  addUserTurn(text);
  try {
    const intent = await api.resolve(text, state.active);
    if (intent.kind === 'help') {
      thread.append(el('div', { class: 'turn' }, [notice({
        tone: 'info',
        title: 'Type a ticker to start',
        message: 'NVDA, $AAPL, or "what do you think about Nvidia?" — then ask anything '
          + 'about the filings.',
      })]));
      scrollToEnd();
    } else if (intent.kind === 'brief') {
      await runBrief(intent.ticker);
    } else {
      await runAsk(intent.ticker, intent.question || text);
    }
  } catch (error) {
    thread.append(el('div', { class: 'turn' }, [notice({
      tone: 'error',
      title: 'Request failed',
      message: 'The API did not respond.',
      actionable: 'Check that the server is running.',
    })]));
  } finally {
    state.busy = false;
    input.focus();
  }
}

// --- boot --------------------------------------------------------------------

function renderDemoChips() {
  const host = document.getElementById('demo-chips');
  for (const ticker of DEMO_TICKERS) {
    host.append(el('button', {
      type: 'button', class: 'btn', text: ticker, onclick: () => submit(ticker),
    }));
  }
}

async function boot() {
  input = composer({ onSubmit: submit });
  renderDemoChips();
  printer.install();

  document.getElementById('copy-brief').addEventListener('click', async () => {
    const entry = state.briefs.get(state.active);
    if (!entry) return;
    const button = document.getElementById('copy-brief');
    try {
      await navigator.clipboard.writeText(printer.toMarkdown(entry));
      button.textContent = 'Copied';
      setTimeout(() => { button.textContent = 'Copy brief'; }, 1600);
    } catch {
      button.textContent = 'Copy failed';
      setTimeout(() => { button.textContent = 'Copy brief'; }, 1600);
    }
  });

  try {
    state.health = await api.health();
  } catch {
    state.health = null;
  }
  environmentStrip(document.getElementById('environment'), state.health);

  const deepLink = decodeURIComponent(window.location.hash.replace(/^#\/?/, '')).trim();
  if (deepLink) submit(deepLink.toUpperCase());
  else input.focus();
}

boot();

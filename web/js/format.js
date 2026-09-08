// Display formatting. Every value is treated as nullable: the backend's
// dataclasses have changed shape once already, and a missing number must
// render as an em dash rather than "NaN" or "undefined".

const DASH = '—';

export function money(value, unit = 'USD') {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  if (unit === 'USD/shares') return `$${value.toFixed(2)}`;
  const scales = [[1e12, 'T'], [1e9, 'B'], [1e6, 'M']];
  for (const [cut, suffix] of scales) {
    if (Math.abs(value) >= cut) return `$${(value / cut).toFixed(2)}${suffix}`;
  }
  return `$${value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function ratio(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  return value.toLocaleString('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

export function signed(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  return `${value >= 0 ? '+' : ''}${value.toFixed(digits)}`;
}

export function bytes(value) {
  if (!value) return DASH;
  const mb = value / (1024 * 1024);
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`;
}

export function timestamp(iso) {
  if (!iso) return DASH;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())} `
    + `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())} UTC`;
}

export function seconds(value) {
  if (value === null || value === undefined) return DASH;
  return `${Math.round(value)}s`;
}

export function cost(value) {
  if (value === null || value === undefined) return DASH;
  return `$${Number(value).toFixed(2)}`;
}

export function count(value) {
  return (value ?? 0).toLocaleString('en-US');
}

export const EM_DASH = DASH;

// Plain-English labels for 8-K item codes. The codes alone identify the event
// before a word of the filing is read - that is the whole point of the layer.
export const ITEM_LABELS = {
  '1.01': 'Material agreement entered',
  '1.02': 'Material agreement terminated',
  '2.01': 'Completion of acquisition or disposition',
  '2.02': 'Results of operations (earnings)',
  '2.03': 'Direct financial obligation created',
  '2.05': 'Costs of exit or disposal',
  '3.01': 'Listing / delisting notice',
  '4.01': 'Change of accountant',
  '5.02': 'Director or officer departure / appointment',
  '5.07': 'Shareholder vote results',
  '7.01': 'Regulation FD disclosure',
  '8.01': 'Other material events',
  '9.01': 'Financial statements and exhibits',
};

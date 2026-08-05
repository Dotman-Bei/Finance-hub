/** Display formatters. Finance UI: always tabular numerals, never lossy. */

const compactFmt = new Intl.NumberFormat('en-US', {
  notation: 'compact',
  maximumFractionDigits: 1,
})

export const compact = (n) => (Number.isFinite(n) ? compactFmt.format(n) : '—')

export const integer = (n) =>
  Number.isFinite(n) ? new Intl.NumberFormat('en-US').format(Math.round(n)) : '—'

export function currency(amount, code = 'USD', { compact: useCompact = false } = {}) {
  if (!Number.isFinite(amount)) return '—'
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: code,
    notation: useCompact ? 'compact' : 'standard',
    maximumFractionDigits: useCompact ? 1 : 2,
    minimumFractionDigits: useCompact ? 0 : 2,
  }).format(amount)
}

export const percent = (ratio, digits = 1) =>
  Number.isFinite(ratio) ? `${(ratio * 100).toFixed(digits)}%` : '—'

export const signedPercent = (ratio, digits = 1) => {
  if (!Number.isFinite(ratio)) return '—'
  const sign = ratio > 0 ? '+' : ''
  return `${sign}${(ratio * 100).toFixed(digits)}%`
}

const rtf = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })

export function relativeTime(input) {
  if (!input) return '—'
  const then = new Date(input).getTime()
  if (Number.isNaN(then)) return '—'

  const seconds = Math.round((then - Date.now()) / 1000)
  const units = [
    ['second', 60],
    ['minute', 60],
    ['hour', 24],
    ['day', 7],
    ['week', 4.35],
    ['month', 12],
    ['year', Infinity],
  ]

  let value = seconds
  for (const [unit, step] of units) {
    if (Math.abs(value) < step) return rtf.format(Math.round(value), unit)
    value /= step
  }
  return rtf.format(Math.round(value), 'year')
}

export const shortDate = (input) =>
  input
    ? new Date(input).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
    : '—'

export const fullDateTime = (input) =>
  input
    ? new Date(input).toLocaleString('en-US', {
        month: 'short',
        day: 'numeric',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      })
    : '—'

/** Short, human id for display: 8ac31f42-… → 8AC31F42 */
export const shortId = (id) => (id ? String(id).split('-')[0].toUpperCase() : '—')

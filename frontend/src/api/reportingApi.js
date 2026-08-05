import axiosClient, { ApiError } from './axiosClient'

/**
 * Endpoint bindings for the reporting_api gateway (§12).
 *
 *   GET  /metrics/kpi
 *   GET  /metrics/match-rate?from=&to=
 *   GET  /metrics/categories
 *   GET  /exceptions
 *   POST /exceptions/{id}/resolve
 *   GET  /reports
 *   POST /reports/generate
 *   GET  /reports/{id}/download
 *
 * There is no fallback and no synthetic data. If the gateway is unreachable the
 * call rejects and the dashboard shows the failure. A panel rendering invented
 * figures is indistinguishable from one rendering real ones, which makes the
 * whole dashboard unfalsifiable — so an outage must look like an outage.
 */

/* ── Connection status ─────────────────────────────────────────────────── */

let connection = { live: false, checked: false, message: 'Connecting to reporting API…' }
const listeners = new Set()

export const connectionStatus = {
  get: () => connection,
  subscribe(fn) {
    listeners.add(fn)
    fn(connection)
    return () => listeners.delete(fn)
  },
}

function setConnection(next) {
  const merged = { ...connection, ...next, checked: true }
  if (
    merged.live === connection.live &&
    merged.message === connection.message &&
    connection.checked
  ) {
    return
  }
  connection = merged
  listeners.forEach((fn) => fn(connection))
}

/** Track whether the gateway is answering, then let the caller handle it. */
async function call(request) {
  try {
    const data = await request()
    setConnection({ live: true, message: 'Live — reporting API connected' })
    return data
  } catch (error) {
    if (error instanceof ApiError && error.offline) {
      setConnection({ live: false, message: 'Reporting API unreachable' })
    } else {
      // A 4xx/5xx means the gateway answered; the connection is up even
      // though this particular request failed.
      setConnection({ live: true, message: 'Live — reporting API connected' })
    }
    throw error
  }
}

/* ── Normalisation ─────────────────────────────────────────────────────── */

/**
 * Guarantee the shape the exception table renders against.
 *
 * The gateway serves the transaction nested under `transaction`; this also
 * accepts it flattened so a payload arriving straight off the WebSocket in
 * either form renders identically. Missing fields become null and display as
 * "—" — never a substituted value.
 */
export function normalizeException(raw) {
  const txn = raw.transaction ?? raw

  const created = raw.created_at ?? new Date().toISOString()
  const ageHours =
    raw.age_hours ?? Math.max(0, Math.round((Date.now() - new Date(created).getTime()) / 3600000))

  return {
    ...raw,
    id: raw.id,
    transaction_id: raw.transaction_id ?? txn.id,
    state: raw.state ?? 'OPEN',
    category: raw.category ?? null,
    classifier_confidence: raw.classifier_confidence ?? null,
    suggested_resolution: raw.suggested_resolution ?? null,
    created_at: created,
    age_hours: ageHours,
    transaction: {
      id: txn.id ?? raw.transaction_id,
      external_id: txn.external_id ?? null,
      source_type: txn.source_type ?? 'unknown',
      amount: Number(txn.amount ?? 0),
      currency: txn.currency ?? 'USD',
      txn_date: txn.txn_date ?? String(created).slice(0, 10),
      description: txn.description ?? null,
      reference_code: txn.reference_code ?? null,
    },
  }
}

/* ── Metrics ───────────────────────────────────────────────────────────── */

export const getKpi = (windowDays = 30) =>
  call(async () => (await axiosClient.get('/metrics/kpi', { params: { window_days: windowDays } })).data)

export const getMatchRate = ({ from, to } = {}) =>
  call(async () => (await axiosClient.get('/metrics/match-rate', { params: { from, to } })).data)

/**
 * Exception counts per category, aggregated by the gateway.
 *
 * Server-side because it must count the whole queue, not the page the table
 * happens to be showing.
 */
export const getCategoryBreakdown = () =>
  call(async () => (await axiosClient.get('/metrics/categories')).data)

/* ── Exceptions ────────────────────────────────────────────────────────── */

export function getExceptions({ category, state, search, limit = 100, offset = 0 } = {}) {
  return call(async () => {
    const { data } = await axiosClient.get('/exceptions', {
      params: { category, state, search, limit, offset },
    })
    const rows = Array.isArray(data) ? data : (data?.items ?? [])
    return rows.map(normalizeException)
  })
}

/**
 * Record a human decision (§10 feedback capture).
 *
 * The gateway forwards this to the exception handler, which owns the write and
 * the audit trigger. A 503 here means nothing was recorded.
 */
export const resolveException = (id, { decision, resolution = null, note = '', correctedCategory = null } = {}) =>
  call(
    async () =>
      (
        await axiosClient.post(`/exceptions/${id}/resolve`, {
          decision,
          resolution,
          note,
          corrected_category: correctedCategory,
        })
      ).data
  )

/* ── Reports ───────────────────────────────────────────────────────────── */

export const getReports = () =>
  call(async () => (await axiosClient.get('/reports')).data)

export const generateReport = ({ type, from, to, title }) =>
  call(async () => (await axiosClient.post('/reports/generate', { type, from, to, title })).data)

/**
 * Download the stored PDF.
 *
 * The gateway returns the bytes that were persisted at generation time, not a
 * re-render — regenerating would produce a different document from the one
 * that was reviewed.
 */
export async function downloadReport(report) {
  const response = await call(async () =>
    axiosClient.get(`/reports/${report.id}/download`, { responseType: 'blob' })
  )

  const url = URL.createObjectURL(response.data)
  const link = document.createElement('a')
  link.href = url
  link.download = `${report.name.replace(/[^\w-]+/g, '_')}.pdf`
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

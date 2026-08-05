import { useState } from 'react'
import Icon from './ui/Icon'
import { REPORT_TYPES } from '../lib/constants'
import { fullDateTime, relativeTime } from '../lib/format'

const TYPE_ICON = {
  RECONCILIATION_SUMMARY: 'scale',
  EXCEPTION_LOG: 'alert',
  MATCH_RATE_ANALYTICS: 'pulse',
  AUDIT_TRAIL: 'history',
}

const isoDaysAgo = (n) => new Date(Date.now() - n * 86400000).toISOString().slice(0, 10)

/**
 * Reports (§12) — trigger a ReportLab render on the gateway and download the
 * persisted PDF. Every generated report keeps its ID for provenance.
 */
export default function ReportsPanel({
  reports = [],
  loading,
  canGenerate,
  generating,
  downloadingId,
  onGenerate,
  onDownload,
}) {
  const [type, setType] = useState(REPORT_TYPES[0].value)
  const [from, setFrom] = useState(isoDaysAgo(30))
  const [to, setTo] = useState(isoDaysAgo(0))

  const submit = (event) => {
    event.preventDefault()
    const label = REPORT_TYPES.find((t) => t.value === type)?.label ?? 'Report'
    onGenerate({ type, from, to, title: `${label} — ${from} to ${to}` })
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
      {/* Generator */}
      <form onSubmit={submit} className="glass-strong h-fit p-5">
        <p className="eyebrow">Generate</p>
        <h3 className="mt-2.5 text-title text-on-surface">Audit-ready PDF</h3>
        <p className="mt-1.5 text-[0.8125rem] leading-relaxed text-on-surface-variant">
          Jinja2 lays out the document, ReportLab renders it, and the gateway stores it against a
          report ID.
        </p>

        <div className="mt-5 space-y-3">
          <label className="block">
            <span className="text-[0.625rem] font-bold uppercase tracking-[0.1em] text-on-surface-muted">
              Report type
            </span>
            <select
              value={type}
              onChange={(e) => setType(e.target.value)}
              className="mt-1.5 w-full rounded-2xl border border-outline-variant bg-surface px-3.5 py-2.5
                         text-[0.8125rem] font-semibold text-on-surface focus:border-primary focus:outline-none"
            >
              {REPORT_TYPES.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>

          <div className="grid grid-cols-2 gap-3">
            {[
              ['From', from, setFrom],
              ['To', to, setTo],
            ].map(([label, value, setValue]) => (
              <label key={label} className="block">
                <span className="text-[0.625rem] font-bold uppercase tracking-[0.1em] text-on-surface-muted">
                  {label}
                </span>
                <input
                  type="date"
                  value={value}
                  max={isoDaysAgo(0)}
                  onChange={(e) => setValue(e.target.value)}
                  className="mt-1.5 w-full rounded-2xl border border-outline-variant bg-surface px-3.5 py-2.5
                             text-[0.8125rem] font-semibold text-on-surface focus:border-primary focus:outline-none"
                />
              </label>
            ))}
          </div>
        </div>

        <button
          type="submit"
          disabled={!canGenerate || generating || from > to}
          className="btn-primary mt-5 w-full py-2.5"
        >
          <Icon name={generating ? 'refresh' : 'document'} size={13} className={generating ? 'animate-spin' : ''} />
          {generating ? 'Rendering…' : 'Generate report'}
        </button>

        {from > to && (
          <p className="mt-2 text-[0.6875rem] font-semibold text-quarantined">
            The start date must fall on or before the end date.
          </p>
        )}

        {!canGenerate && (
          <p className="mt-3 flex items-start gap-2 text-[0.6875rem] leading-snug text-on-surface-muted">
            <Icon name="shield" size={12} className="mt-0.5 shrink-0" />
            Report generation is not available to this role.
          </p>
        )}
      </form>

      {/* History */}
      <div className="glass-strong overflow-hidden">
        <div className="flex items-center justify-between gap-3 border-b border-outline-variant/50 px-5 py-4">
          <div>
            <p className="eyebrow">History</p>
            <h3 className="mt-1.5 text-title text-on-surface">Generated reports</h3>
          </div>
          <span className="chip border-outline-variant bg-surface/70 text-on-surface-variant">
            {reports.length} stored
          </span>
        </div>

        {loading ? (
          <div className="space-y-3 p-5">
            {Array.from({ length: 3 }, (_, i) => (
              <div key={i} className="skeleton h-16 w-full" />
            ))}
          </div>
        ) : reports.length === 0 ? (
          <div className="flex flex-col items-center px-6 py-14 text-center">
            <span className="flex h-12 w-12 items-center justify-center rounded-full bg-surface-dim text-on-surface-muted">
              <Icon name="document" size={20} />
            </span>
            <p className="mt-4 text-title text-on-surface">No reports yet</p>
            <p className="mt-1.5 max-w-xs text-[0.8125rem] text-on-surface-variant">
              Generate one from the panel beside this list — it will be persisted with an ID for
              provenance.
            </p>
          </div>
        ) : (
          <ul>
            {reports.map((report) => (
              <li
                key={report.id}
                className="flex flex-wrap items-center gap-4 border-b border-outline-variant/50 px-5 py-4
                           transition-colors duration-200 last:border-b-0 hover:bg-surface-dim/60"
              >
                <span
                  className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl
                             border border-outline-variant/60 bg-surface text-on-surface"
                >
                  <Icon name={TYPE_ICON[report.type] ?? 'document'} size={16} />
                </span>

                <div className="min-w-[12rem] flex-1">
                  <p className="truncate text-[0.875rem] font-bold tracking-tight-ui text-on-surface">
                    {report.name}
                  </p>
                  <p className="mt-0.5 truncate text-[0.75rem] font-medium text-on-surface-muted">
                    {report.period} · {report.generated_by}
                  </p>
                </div>

                <div className="hidden text-right sm:block">
                  <p className="text-[0.75rem] font-semibold text-on-surface-variant">
                    {relativeTime(report.generated_at)}
                  </p>
                  <p
                    className="tabular text-[0.6875rem] font-medium text-on-surface-muted"
                    title={fullDateTime(report.generated_at)}
                  >
                    {report.size_kb} KB
                  </p>
                </div>

                <button
                  type="button"
                  onClick={() => onDownload(report)}
                  disabled={downloadingId === report.id}
                  className="btn-ghost px-3.5 py-2"
                >
                  <Icon
                    name={downloadingId === report.id ? 'refresh' : 'download'}
                    size={13}
                    className={downloadingId === report.id ? 'animate-spin' : ''}
                  />
                  <span className="hidden sm:inline">PDF</span>
                </button>
              </li>
            ))}
          </ul>
        )}

        <p className="flex items-center gap-2 border-t border-outline-variant/50 px-5 py-3 text-[0.6875rem] font-medium text-on-surface-muted">
          <Icon name="shield" size={12} />
          Reports include reconciliation summaries, exception logs and match-rate analytics, and are
          retained for audit.
        </p>
      </div>
    </div>
  )
}

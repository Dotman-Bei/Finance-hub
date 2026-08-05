import Icon from './ui/Icon'
import { fullDateTime, percent, relativeTime } from '../lib/format'

const PIPELINE = [
  { label: 'Ingest', icon: 'layers', note: 'Kafka stream' },
  { label: 'Validate', icon: 'shield', note: 'Pydantic + GE' },
  { label: 'Match', icon: 'scale', note: 'Rule → ML' },
  { label: 'Report', icon: 'pulse', note: 'This dashboard' },
]

/**
 * Hero band. Cursor-reactive wash behind an editorial headline, with the live
 * pipeline state read straight off the KPI roll-up.
 */
export default function Hero({ kpi, live, feedLive }) {
  const status = kpi?.reconciliation_status
  const healthy = status === 'HEALTHY'
  // UNKNOWN is not ATTENTION: it means no pass has run yet, which is a
  // different fact from a pass having gone wrong.
  const unknown = !status || status === 'UNKNOWN'
  const dot = unknown ? 'bg-on-surface-muted' : healthy ? 'bg-matched' : 'bg-exception'

  return (
    <section className="relative isolate overflow-hidden pb-rhythm-2 pt-40">
      <div className="cursor-aura pointer-events-none absolute inset-0 -z-10" aria-hidden="true" />
      <div
        className="pointer-events-none absolute -top-40 left-1/2 -z-10 h-[36rem] w-[68rem] -translate-x-1/2
                   animate-drift rounded-full bg-grad-subtle blur-3xl"
        aria-hidden="true"
      />

      <div className="mx-auto w-full max-w-6xl px-6">
        {/* Live status pill */}
        <div className="flex justify-center">
          <span
            className="inline-flex items-center gap-2.5 rounded-pill border border-outline-variant/50
                       bg-surface/70 py-1.5 pl-2.5 pr-4 text-caption text-on-surface-variant
                       shadow-glass backdrop-blur-glass"
          >
            <span className="relative flex h-2 w-2 items-center justify-center">
              <span
                className={`absolute inline-flex h-2 w-2 rounded-full ${dot} ${
                  feedLive ? 'animate-pulse-ring' : ''
                }`}
              />
              <span className={`relative inline-flex h-2 w-2 rounded-full ${dot}`} />
            </span>
            {live ? 'Reporting API live' : 'Reporting API unreachable'}
            <span className="h-3 w-px bg-outline-variant" />
            {kpi?.last_run_at
              ? `Last pass ${relativeTime(kpi.last_run_at)}`
              : 'No reconciliation pass yet'}
          </span>
        </div>

        <h1 className="mx-auto mt-8 max-w-4xl text-center text-display">
          Reconcile everything.
          <br />
          <span className="text-gradient-energy">Chase nothing.</span>
        </h1>

        <p className="mx-auto mt-6 max-w-xl text-center text-[1rem] leading-relaxed text-on-surface-variant">
          A hybrid rule-plus-machine-learning engine matches high-volume transactions across banks,
          payment gateways and your ERP — then explains every item it could not.
        </p>

        {/* Pipeline strip */}
        <div className="mt-rhythm-2 flex flex-wrap items-stretch justify-center gap-2.5">
          {PIPELINE.map((stage, index) => (
            <div key={stage.label} className="flex items-center gap-2.5">
              <div className="glass-quiet flex items-center gap-3 rounded-pill py-2.5 pl-3.5 pr-5">
                <span
                  className="flex h-7 w-7 items-center justify-center rounded-full bg-surface
                             text-on-surface shadow-[0_1px_3px_rgba(16,16,20,0.10)]"
                >
                  <Icon name={stage.icon} size={14} />
                </span>
                <span className="leading-tight">
                  <span className="block text-[0.8125rem] font-bold tracking-tight-ui text-on-surface">
                    {stage.label}
                  </span>
                  <span className="block text-[0.6875rem] font-medium text-on-surface-muted">
                    {stage.note}
                  </span>
                </span>
              </div>
              {index < PIPELINE.length - 1 && (
                <Icon name="chevron" size={14} className="-rotate-90 text-on-surface-muted" />
              )}
            </div>
          ))}
        </div>

        {/* Objective read-outs */}
        <dl className="mx-auto mt-rhythm grid max-w-3xl grid-cols-2 gap-x-8 gap-y-6 sm:grid-cols-4">
          {[
            { label: 'Match rate', value: percent(kpi?.match_rate, 1) },
            { label: 'Detection rate', value: percent(kpi?.validation_detection_rate, 1) },
            { label: 'Auto-resolved', value: percent(kpi?.auto_resolved_rate, 0) },
            { label: 'p95 latency', value: `${kpi?.avg_reconcile_latency_ms ?? '—'} ms` },
          ].map((stat) => (
            <div key={stat.label} className="text-center">
              <dt className="text-[0.6875rem] font-semibold uppercase tracking-[0.12em] text-on-surface-muted">
                {stat.label}
              </dt>
              <dd className="tabular mt-1.5 text-[1.375rem] font-extrabold tracking-tighter text-on-surface">
                {stat.value}
              </dd>
            </div>
          ))}
        </dl>

        <p className="mt-8 text-center text-[0.6875rem] font-medium text-on-surface-muted">
          Next scheduled pass {fullDateTime(kpi?.next_run_at)}
        </p>
      </div>
    </section>
  )
}

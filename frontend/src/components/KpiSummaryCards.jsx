import Icon from './ui/Icon'
import { compact, currency, integer, percent, relativeTime, signedPercent } from '../lib/format'

/** Sparkline drawn straight from the series — no chart library for 40px of trend. */
function Sparkline({ values = [], stroke = '#FF8A65', id }) {
  if (values.length < 2) return null

  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const step = 100 / (values.length - 1)

  const points = values.map((v, i) => [i * step, 32 - ((v - min) / span) * 28])
  const line = points.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(2)} ${y.toFixed(2)}`).join(' ')
  const area = `${line} L100 32 L0 32 Z`

  return (
    <svg
      viewBox="0 0 100 32"
      preserveAspectRatio="none"
      className="h-8 w-full"
      aria-hidden="true"
    >
      <defs>
        <linearGradient id={`spark-${id}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={stroke} stopOpacity="0.22" />
          <stop offset="100%" stopColor={stroke} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill={`url(#spark-${id})`} />
      <path
        d={line}
        fill="none"
        stroke={stroke}
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  )
}

function Delta({ value, invert = false }) {
  if (!Number.isFinite(value)) return null

  const good = invert ? value < 0 : value > 0
  const flat = Math.abs(value) < 0.0005

  return (
    <span
      className={`inline-flex items-center gap-1 text-[0.6875rem] font-bold tracking-tight-ui ${
        flat ? 'text-on-surface-muted' : good ? 'text-matched' : 'text-quarantined'
      }`}
    >
      {!flat && <Icon name={value > 0 ? 'trendUp' : 'trendDown'} size={12} strokeWidth={2} />}
      {signedPercent(value)}
      <span className="font-medium text-on-surface-muted">vs prior 30d</span>
    </span>
  )
}

function Card({ card, index }) {
  return (
    <article
      style={{ animationDelay: `${index * 70}ms` }}
      className="glass group animate-fade-up p-5 transition-all duration-500
                 hover:-translate-y-1 hover:border-outline-variant hover:shadow-glass-lg"
    >
      <div className="flex items-start justify-between gap-3">
        <p className="eyebrow">{card.label}</p>
        <span
          className="flex h-8 w-8 items-center justify-center rounded-full border border-outline-variant/60
                     bg-surface/80 text-on-surface transition-colors duration-300 group-hover:border-primary-200
                     group-hover:text-primary-600"
        >
          <Icon name={card.icon} size={14} />
        </span>
      </div>

      <p className="tabular mt-4 text-[2rem] font-extrabold leading-none tracking-tightest text-on-surface">
        {card.value}
      </p>

      <p className="mt-2 text-[0.75rem] font-medium leading-snug text-on-surface-variant">
        {card.caption}
      </p>

      <div className="mt-4 min-h-[2rem]">
        {card.spark ? (
          <Sparkline values={card.spark} stroke={card.color} id={card.key} />
        ) : (
          card.footer
        )}
      </div>

      <div className="mt-3 border-t border-outline-variant/50 pt-3">
        {card.delta !== undefined ? (
          <Delta value={card.delta} invert={card.invertDelta} />
        ) : (
          <span className="text-[0.6875rem] font-medium text-on-surface-muted">{card.note}</span>
        )}
      </div>
    </article>
  )
}

function CardSkeleton() {
  return (
    <div className="glass p-5">
      <div className="skeleton h-3 w-24" />
      <div className="skeleton mt-5 h-8 w-32" />
      <div className="skeleton mt-3 h-3 w-40" />
      <div className="skeleton mt-5 h-8 w-full" />
    </div>
  )
}

/**
 * Live KPI tiles (§12): total volume, overall match rate, open exceptions and
 * the reconciliation status read-out.
 */
export default function KpiSummaryCards({ kpi, series = [], loading }) {
  if (loading) {
    return (
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }, (_, i) => (
          <CardSkeleton key={i} />
        ))}
      </div>
    )
  }

  // Loaded, but nothing came back. Skeletons here would read as "still
  // loading" forever; say plainly that there is no data rather than implying
  // work in progress or, worse, showing figures that were never computed.
  if (!kpi) {
    return (
      <div className="glass flex flex-col items-center px-6 py-14 text-center">
        <span className="flex h-12 w-12 items-center justify-center rounded-full bg-surface-dim text-on-surface-muted">
          <Icon name="pulse" size={20} />
        </span>
        <p className="mt-4 text-title text-on-surface">No metrics available</p>
        <p className="mt-1.5 max-w-md text-[0.8125rem] leading-relaxed text-on-surface-variant">
          The reporting API returned no KPI data. Nothing is estimated in its
          place — once the gateway is reachable and a reconciliation pass has
          run, the figures appear here.
        </p>
      </div>
    )
  }

  const recent = series.slice(-30)
  const healthy = kpi.reconciliation_status === 'HEALTHY'

  const cards = [
    {
      key: 'volume',
      label: 'Transaction volume',
      icon: 'layers',
      value: integer(kpi.total_transactions),
      caption: `${currency(kpi.total_value, kpi.currency, { compact: true })} settled across bank, gateway and ERP feeds`,
      spark: recent.map((r) => r.volume),
      color: '#0A84FF',
      delta: kpi.volume_delta,
    },
    {
      key: 'match-rate',
      label: 'Overall match rate',
      icon: 'scale',
      value: percent(kpi.match_rate, 1),
      caption: `Rule layer first, ML layer on the remainder, above a ${percent(0.85, 0)} confidence floor`,
      spark: recent.map((r) => r.match_rate),
      color: '#0F9E8E',
      delta: kpi.match_rate_delta,
    },
    {
      key: 'exceptions',
      label: 'Open exceptions',
      icon: 'alert',
      value: integer(kpi.open_exceptions),
      caption: `${percent(kpi.auto_resolved_rate, 0)} of prior exceptions cleared by a suggested resolution`,
      spark: recent.map((r) => r.unmatched),
      color: '#F5A524',
      delta: kpi.open_exceptions_delta,
      invertDelta: true,
    },
    {
      key: 'status',
      label: 'Reconciliation status',
      icon: 'shield',
      value: healthy ? 'Healthy' : 'Attention',
      caption: `Validation quarantined ${integer(kpi.quarantined_today)} malformed records today at a ${percent(kpi.validation_detection_rate, 1)} detection rate`,
      note: `Last pass ${relativeTime(kpi.last_run_at)} · p95 ${kpi.avg_reconcile_latency_ms} ms`,
      footer: (
        <div className="flex items-center gap-2">
          <span
            className={`chip ${
              healthy
                ? 'border-[#BFE9E2] bg-[#F0FBF9] text-[#0B7A6E]'
                : 'border-[#FDE6C7] bg-[#FFF9F0] text-[#A9651A]'
            }`}
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${healthy ? 'bg-matched' : 'bg-exception'}`}
            />
            {healthy ? 'All gates passing' : 'Gate degraded'}
          </span>
          <span className="text-[0.6875rem] font-semibold text-on-surface-muted">
            {compact(kpi.quarantined_today)} quarantined
          </span>
        </div>
      ),
    },
  ]

  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      {cards.map((card, index) => (
        <Card key={card.key} card={card} index={index} />
      ))}
    </div>
  )
}

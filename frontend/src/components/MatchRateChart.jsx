import { useMemo, useState } from 'react'
import {
  Area,
  Bar,
  BarChart,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { ArcElement, Chart as ChartJS, Legend, Tooltip as ChartTooltip } from 'chart.js'
import { Doughnut } from 'react-chartjs-2'

import Icon from './ui/Icon'
import { RANGE_PRESETS, categoryMeta } from '../lib/constants'
import { compact, integer, percent, shortDate } from '../lib/format'

ChartJS.register(ArcElement, ChartTooltip, Legend)

const TABS = [
  { id: 'trend', label: 'Trend' },
  { id: 'volume', label: 'Volume' },
  { id: 'mix', label: 'Mix' },
]

const AXIS = {
  stroke: '#9A9AA5',
  fontSize: 11,
  fontWeight: 600,
  fontFamily: '"Plus Jakarta Sans", sans-serif',
}

/** Glass tooltip so charts inherit the surface language. */
function GlassTooltip({ active, payload, label, formatter }) {
  if (!active || !payload?.length) return null

  return (
    <div className="glass-strong min-w-[11rem] p-3">
      <p className="text-[0.6875rem] font-bold uppercase tracking-[0.1em] text-on-surface-muted">
        {shortDate(label)}
      </p>
      <ul className="mt-2 space-y-1.5">
        {payload.map((entry) => (
          <li key={entry.dataKey} className="flex items-center justify-between gap-4">
            <span className="flex items-center gap-2 text-[0.75rem] font-medium text-on-surface-variant">
              <span
                className="h-2 w-2 rounded-full"
                style={{ background: entry.color || entry.stroke }}
              />
              {entry.name}
            </span>
            <span className="tabular text-[0.75rem] font-bold text-on-surface">
              {formatter(entry.value, entry.dataKey)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function ChartFrame({ children }) {
  return (
    <div className="h-[19rem] w-full">
      <ResponsiveContainer width="100%" height="100%">
        {children}
      </ResponsiveContainer>
    </div>
  )
}

/**
 * Match-rate analytics (§12) — a tabbed glass pane holding the Recharts
 * time-series and bar views plus a Chart.js breakdown of exception mix.
 */
export default function MatchRateChart({ series = [], categories = [], loading }) {
  const [tab, setTab] = useState('trend')
  const [range, setRange] = useState('30d')

  const data = useMemo(() => {
    const days = RANGE_PRESETS.find((p) => p.value === range)?.days ?? 30
    return series.slice(-days)
  }, [series, range])

  const totals = useMemo(() => {
    const sum = (key) => data.reduce((acc, row) => acc + row[key], 0)
    const matched = sum('matched')
    const volume = sum('volume') || 1
    return {
      volume: sum('volume'),
      matched,
      unmatched: sum('unmatched'),
      rule: sum('rule_matched'),
      ml: sum('ml_matched'),
      rate: matched / volume,
    }
  }, [data])

  const doughnut = useMemo(
    () => ({
      labels: categories.map((c) => categoryMeta(c.category).label),
      datasets: [
        {
          data: categories.map((c) => c.count),
          backgroundColor: categories.map((c) => categoryMeta(c.category).color),
          borderColor: '#FFFFFF',
          borderWidth: 3,
          hoverOffset: 8,
        },
      ],
    }),
    [categories]
  )

  const doughnutOptions = useMemo(
    () => ({
      cutout: '68%',
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(255,255,255,0.92)',
          titleColor: '#000',
          bodyColor: '#6A6A75',
          borderColor: '#E7E7EC',
          borderWidth: 1,
          padding: 12,
          cornerRadius: 14,
          displayColors: false,
          titleFont: { family: '"Plus Jakarta Sans", sans-serif', weight: '700', size: 12 },
          bodyFont: { family: '"Plus Jakarta Sans", sans-serif', weight: '600', size: 12 },
          callbacks: {
            label: (ctx) => `${ctx.parsed} exceptions`,
          },
        },
      },
    }),
    []
  )

  if (loading) {
    return (
      <div className="glass-strong p-6">
        <div className="skeleton h-9 w-56" />
        <div className="skeleton mt-6 h-[19rem] w-full" />
      </div>
    )
  }

  return (
    <div className="glass-strong overflow-hidden">
      {/* Tabbed glass header */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-outline-variant/50 px-5 py-4">
        <div className="segmented" role="tablist" aria-label="Chart view">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={tab === t.id}
              data-active={tab === t.id}
              onClick={() => setTab(t.id)}
              className="segmented-item"
            >
              {t.label}
            </button>
          ))}
        </div>

        <div className="segmented" role="group" aria-label="Date range">
          {RANGE_PRESETS.map((preset) => (
            <button
              key={preset.value}
              type="button"
              data-active={range === preset.value}
              onClick={() => setRange(preset.value)}
              className="segmented-item"
            >
              {preset.label}
            </button>
          ))}
        </div>
      </div>

      {/* Summary strip */}
      <dl className="grid grid-cols-2 divide-outline-variant/50 border-b border-outline-variant/50 sm:grid-cols-4 sm:divide-x">
        {[
          { label: 'Reconciled', value: percent(totals.rate, 1) },
          { label: 'Volume', value: compact(totals.volume) },
          { label: 'Rule layer', value: compact(totals.rule) },
          { label: 'ML layer', value: compact(totals.ml) },
        ].map((stat) => (
          <div key={stat.label} className="px-5 py-4">
            <dt className="text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-on-surface-muted">
              {stat.label}
            </dt>
            <dd className="tabular mt-1 text-[1.25rem] font-extrabold tracking-tighter text-on-surface">
              {stat.value}
            </dd>
          </div>
        ))}
      </dl>

      <div className="p-5">
        {tab === 'trend' && (
          <ChartFrame>
            <ComposedChart data={data} margin={{ top: 8, right: 8, left: -8, bottom: 0 }}>
              <defs>
                <linearGradient id="rateFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#FF8A65" stopOpacity={0.28} />
                  <stop offset="100%" stopColor="#FF8A65" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="#E7E7EC" strokeDasharray="3 6" vertical={false} />
              <XAxis
                dataKey="date"
                tickFormatter={shortDate}
                tick={AXIS}
                tickLine={false}
                axisLine={false}
                minTickGap={28}
              />
              <YAxis
                domain={[0.8, 1]}
                tickFormatter={(v) => `${Math.round(v * 100)}%`}
                tick={AXIS}
                tickLine={false}
                axisLine={false}
                width={56}
              />
              <Tooltip
                cursor={{ stroke: '#D5D5DC', strokeDasharray: '4 4' }}
                content={<GlassTooltip formatter={(v) => percent(v, 2)} />}
              />
              <Area
                type="monotone"
                dataKey="match_rate"
                name="Match rate"
                stroke="none"
                fill="url(#rateFill)"
              />
              <Line
                type="monotone"
                dataKey="match_rate"
                name="Match rate"
                stroke="#FF6E3F"
                strokeWidth={2.2}
                dot={false}
                activeDot={{ r: 4, strokeWidth: 2, stroke: '#fff' }}
              />
            </ComposedChart>
          </ChartFrame>
        )}

        {tab === 'volume' && (
          <ChartFrame>
            <BarChart data={data} margin={{ top: 8, right: 8, left: -14, bottom: 0 }} barCategoryGap="22%">
              <CartesianGrid stroke="#E7E7EC" strokeDasharray="3 6" vertical={false} />
              <XAxis
                dataKey="date"
                tickFormatter={shortDate}
                tick={AXIS}
                tickLine={false}
                axisLine={false}
                minTickGap={28}
              />
              <YAxis
                tickFormatter={compact}
                tick={AXIS}
                tickLine={false}
                axisLine={false}
                width={48}
              />
              <Tooltip
                cursor={{ fill: 'rgba(16,16,20,0.04)' }}
                content={<GlassTooltip formatter={integer} />}
              />
              <Bar dataKey="rule_matched" name="Rule matched" stackId="v" fill="#0A84FF" radius={[0, 0, 0, 0]} />
              <Bar dataKey="ml_matched" name="ML matched" stackId="v" fill="#7B5BF5" />
              <Bar dataKey="unmatched" name="Unmatched" stackId="v" fill="#FF8A65" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ChartFrame>
        )}

        {tab === 'mix' && (
          <div className="grid items-center gap-8 md:grid-cols-[minmax(0,15rem)_minmax(0,1fr)]">
            <div className="relative mx-auto h-[15rem] w-[15rem]">
              <Doughnut data={doughnut} options={doughnutOptions} />
              <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
                <span className="tabular text-[1.75rem] font-extrabold leading-none tracking-tightest text-on-surface">
                  {integer(categories.reduce((a, c) => a + c.count, 0))}
                </span>
                <span className="mt-1 text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-on-surface-muted">
                  Exceptions
                </span>
              </div>
            </div>

            <ul className="space-y-2.5">
              {categories.map((entry) => {
                const total = categories.reduce((a, c) => a + c.count, 0) || 1
                const meta = categoryMeta(entry.category)
                return (
                  <li key={entry.category}>
                    <div className="flex items-center justify-between gap-4">
                      <span className="flex items-center gap-2.5 text-[0.8125rem] font-semibold text-on-surface">
                        <span
                          className="h-2.5 w-2.5 rounded-full"
                          style={{ background: meta.color }}
                        />
                        {meta.label}
                      </span>
                      <span className="tabular shrink-0 text-[0.8125rem] font-bold text-on-surface">
                        {entry.count}
                        <span className="ml-1.5 font-semibold text-on-surface-muted">
                          {percent(entry.count / total, 0)}
                        </span>
                      </span>
                    </div>
                    <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-surface-sunken">
                      <div
                        className="h-full rounded-full transition-all duration-700"
                        style={{
                          width: `${(entry.count / total) * 100}%`,
                          background: meta.color,
                        }}
                      />
                    </div>
                    <p className="mt-1.5 text-[0.6875rem] leading-snug text-on-surface-muted">
                      {meta.pathway}
                    </p>
                  </li>
                )
              })}
            </ul>
          </div>
        )}
      </div>

      <p className="flex items-center gap-2 border-t border-outline-variant/50 px-5 py-3 text-[0.6875rem] font-medium text-on-surface-muted">
        <Icon name="clock" size={12} />
        Aggregated from <span className="font-bold text-on-surface-variant">matchedrecords</span> and{' '}
        <span className="font-bold text-on-surface-variant">exceptionqueue</span>, cached in Redis
        between polls.
      </p>
    </div>
  )
}

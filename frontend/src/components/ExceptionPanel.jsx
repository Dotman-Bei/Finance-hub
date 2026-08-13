import { useMemo, useState } from 'react'
import Icon from './ui/Icon'
import { CATEGORY_META, EXCEPTION_CATEGORIES, EXCEPTION_STATES, categoryMeta } from '../lib/constants'
import { currency, relativeTime, shortId } from '../lib/format'

const STATE_FILTERS = ['ALL', 'OPEN', 'SUGGESTED', 'RESOLVED', 'REJECTED']
const SORTS = [
  { value: 'newest', label: 'Newest' },
  { value: 'amount', label: 'Amount' },
  { value: 'confidence', label: 'Confidence' },
]

function ConfidenceBar({ value }) {
  // An untriaged row has no classifier confidence yet. Rendering it as 0%
  // states the opposite of the truth — that the classifier looked and was
  // certain of nothing — so say it has not scored it.
  if (value == null) {
    return <span className="text-[0.75rem] font-semibold text-on-surface-muted">Not scored yet</span>
  }

  const pct = Math.round(value * 100)
  const tone = pct >= 85 ? '#0F9E8E' : pct >= 70 ? '#F5A524' : '#E5484D'

  return (
    <div className="flex items-center gap-2">
      <div className="h-1 w-14 overflow-hidden rounded-full bg-surface-sunken">
        <div className="h-full rounded-full" style={{ width: `${pct}%`, background: tone }} />
      </div>
      <span className="tabular text-[0.75rem] font-bold text-on-surface">{pct}%</span>
    </div>
  )
}

/** Expanded drawer: the classifier's suggestion plus the human decision controls. */
function ResolutionDrawer({ item, canResolve, busy, onDecide }) {
  const [editing, setEditing] = useState(false)
  const [detail, setDetail] = useState(item.suggested_resolution?.detail ?? '')
  const [note, setNote] = useState('')

  // The matching engine opens every exception with a `suggested_resolution`
  // already populated — its own near-miss data, under a `matching_engine` key,
  // which Subsystem 3 reads to build features. That object is truthy but
  // carries no pathway until triage fills one in, so presence alone rendered a
  // blank suggestion and hid the awaiting-triage message written for this case.
  // The pathway is what makes it a suggestion.
  const suggestion = item.suggested_resolution?.pathway ? item.suggested_resolution : null
  const meta = categoryMeta(item.category)
  const settled = item.state === 'RESOLVED' || item.state === 'REJECTED'

  return (
    <div className="border-t border-outline-variant/50 bg-surface-variant/60 px-5 py-5">
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,20rem)]">
        {/* Suggestion */}
        <div>
          <p className="eyebrow">Suggested resolution</p>

          {suggestion ? (
            <>
              <p className="mt-2.5 text-[0.9375rem] font-bold tracking-tight-ui text-on-surface">
                {suggestion.pathway}
              </p>

              {editing ? (
                <textarea
                  value={detail}
                  onChange={(e) => setDetail(e.target.value)}
                  rows={3}
                  className="mt-2.5 w-full resize-none rounded-2xl border border-outline-variant bg-surface
                             px-3.5 py-2.5 text-[0.8125rem] leading-relaxed text-on-surface
                             placeholder:text-on-surface-muted focus:border-primary focus:outline-none"
                  placeholder="Describe the amended resolution…"
                />
              ) : (
                <p className="mt-2 max-w-2xl text-[0.8125rem] leading-relaxed text-on-surface-variant">
                  {detail}
                </p>
              )}

              {suggestion.fields && (
                <dl className="mt-4 flex flex-wrap gap-x-8 gap-y-3">
                  {Object.entries(suggestion.fields).map(([key, value]) => (
                    <div key={key}>
                      <dt className="text-[0.625rem] font-bold uppercase tracking-[0.1em] text-on-surface-muted">
                        {key.replace(/_/g, ' ')}
                      </dt>
                      <dd className="tabular mt-0.5 text-[0.8125rem] font-bold text-on-surface">
                        {typeof value === 'number' ? value.toLocaleString() : String(value)}
                      </dd>
                    </div>
                  ))}
                </dl>
              )}
            </>
          ) : (
            <p className="mt-2.5 max-w-lg text-[0.8125rem] leading-relaxed text-on-surface-variant">
              {item.category ? (
                <>
                  Awaiting a suggestion. This item is classified as{' '}
                  <span className="font-semibold text-on-surface">{meta.label.toLowerCase()}</span>,
                  so its pathway will be{' '}
                  <span className="font-semibold text-on-surface">
                    {meta.pathway.toLowerCase()}
                  </span>
                  .
                </>
              ) : (
                // Naming the sweep matters: without it, an untriaged card reads
                // as something the classifier failed on rather than something it
                // has not reached yet.
                <>
                  Awaiting triage. The exception handler sweeps the open queue every two minutes —
                  the category, confidence and a suggested pathway appear here once it reaches this
                  item.
                </>
              )}
            </p>
          )}

          {!settled && canResolve && (
            <input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Add a note for the audit trail (optional)"
              className="mt-4 w-full max-w-lg rounded-pill border border-outline-variant bg-surface
                         px-4 py-2 text-[0.8125rem] text-on-surface placeholder:text-on-surface-muted
                         focus:border-primary focus:outline-none"
            />
          )}
        </div>

        {/* Transaction facts + actions */}
        <div className="glass p-4">
          <p className="eyebrow">Transaction</p>
          <dl className="mt-3 space-y-2.5 text-[0.75rem]">
            {[
              ['External ID', item.transaction.external_id],
              ['Source', item.transaction.source_type.replace(/_/g, ' ')],
              ['Reference', item.transaction.reference_code ?? 'None on record'],
              ['Value date', item.transaction.txn_date],
              ['Queue ID', shortId(item.id)],
            ].map(([label, value]) => (
              <div key={label} className="flex items-baseline justify-between gap-3">
                <dt className="text-on-surface-muted">{label}</dt>
                <dd className="tabular truncate text-right font-bold text-on-surface">{value}</dd>
              </div>
            ))}
          </dl>

          <div className="mt-4 border-t border-outline-variant/60 pt-4">
            {settled ? (
              <p className="flex items-center gap-2 text-[0.75rem] font-semibold text-on-surface-variant">
                <Icon name={item.state === 'RESOLVED' ? 'check' : 'close'} size={13} />
                {EXCEPTION_STATES[item.state].label} {relativeTime(item.resolved_at)}
              </p>
            ) : canResolve ? (
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    onDecide({
                      decision: editing ? 'EDIT' : 'ACCEPT',
                      resolution: suggestion ? { ...suggestion, detail } : null,
                      note,
                    })
                  }
                  className="btn-primary flex-1 px-4 py-2"
                >
                  <Icon name="check" size={13} strokeWidth={2.4} />
                  {editing ? 'Save & resolve' : 'Accept'}
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => setEditing((v) => !v)}
                  className="btn-ghost px-3 py-2"
                  title="Amend the suggested resolution"
                >
                  <Icon name="pencil" size={13} />
                  {editing ? 'Cancel' : 'Edit'}
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => onDecide({ decision: 'REJECT', resolution: null, note })}
                  className="btn-ghost px-3 py-2 text-quarantined"
                  title="Reject this suggestion"
                >
                  <Icon name="close" size={13} />
                </button>
              </div>
            ) : (
              <p className="flex items-start gap-2 text-[0.75rem] leading-snug text-on-surface-muted">
                <Icon name="shield" size={13} className="mt-0.5 shrink-0" />
                Your role has read-only access to the exception queue. Decisions are recorded by
                Finance Managers and System Administrators.
              </p>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function Row({ item, expanded, onToggle, canResolve, busy, onDecide, isNew }) {
  const meta = categoryMeta(item.category)
  const state = EXCEPTION_STATES[item.state]

  return (
    <li
      className={`overflow-hidden border-b border-outline-variant/50 last:border-b-0 ${
        isNew ? 'animate-fade-in bg-primary-50/50' : ''
      }`}
    >
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className="grid w-full grid-cols-[minmax(0,1fr)_auto] items-center gap-4 px-5 py-4 text-left
                   transition-colors duration-200 hover:bg-surface-dim/60 sm:grid-cols-[minmax(0,1fr)_10rem_9rem_7rem_1.5rem]"
      >
        {/* Description + category */}
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className={`chip ${meta.chip}`}>{meta.short}</span>
            <span className={`chip ${state.chip}`}>{state.label}</span>
            {isNew && (
              <span className="chip border-primary-200 bg-primary-50 text-primary-700">New</span>
            )}
          </div>
          <p className="mt-2 truncate text-[0.875rem] font-bold tracking-tight-ui text-on-surface">
            {item.transaction.description}
          </p>
          <p className="tabular mt-0.5 truncate text-[0.75rem] font-medium text-on-surface-muted">
            {item.transaction.external_id} · {item.transaction.source_type.replace(/_/g, ' ')} ·{' '}
            {relativeTime(item.created_at)}
          </p>
        </div>

        {/* Amount */}
        <div className="hidden text-right sm:block">
          <p className="tabular text-[0.9375rem] font-extrabold tracking-tighter text-on-surface">
            {currency(item.transaction.amount, item.transaction.currency)}
          </p>
          <p className="text-[0.6875rem] font-semibold text-on-surface-muted">
            {item.transaction.reference_code ?? 'no reference'}
          </p>
        </div>

        {/* Confidence */}
        <div className="hidden sm:block">
          <p className="text-[0.625rem] font-bold uppercase tracking-[0.1em] text-on-surface-muted">
            Confidence
          </p>
          <div className="mt-1">
            <ConfidenceBar value={item.classifier_confidence} />
          </div>
        </div>

        {/* Age */}
        <p className="tabular hidden text-[0.75rem] font-semibold text-on-surface-variant sm:block">
          {item.age_hours < 24
            ? `${item.age_hours}h old`
            : `${Math.round(item.age_hours / 24)}d old`}
        </p>

        <Icon
          name="chevron"
          size={16}
          className={`justify-self-end text-on-surface-muted transition-transform duration-300 ${
            expanded ? 'rotate-180' : ''
          }`}
        />
      </button>

      {expanded && (
        <ResolutionDrawer item={item} canResolve={canResolve} busy={busy} onDecide={onDecide} />
      )}
    </li>
  )
}

/**
 * Exception queue (§12) — filterable table over `exceptionqueue`, with the
 * classifier's suggested pathway and inline accept / reject / edit. Every
 * decision is feedback: it is captured for the retraining cycle (§11).
 */
export default function ExceptionPanel({
  exceptions = [],
  loading,
  canResolve,
  busyId,
  newIds = new Set(),
  onResolve,
}) {
  const [category, setCategory] = useState('ALL')
  const [state, setState] = useState('ALL')
  const [sort, setSort] = useState('newest')
  const [search, setSearch] = useState('')
  const [expandedId, setExpandedId] = useState(null)

  const rows = useMemo(() => {
    const needle = search.trim().toLowerCase()

    const filtered = exceptions.filter((item) => {
      if (category !== 'ALL' && item.category !== category) return false
      if (state !== 'ALL' && item.state !== state) return false
      if (!needle) return true
      return [
        item.transaction.external_id,
        item.transaction.description,
        item.transaction.reference_code,
        item.category,
      ]
        .filter(Boolean)
        .some((field) => String(field).toLowerCase().includes(needle))
    })

    const comparators = {
      newest: (a, b) => new Date(b.created_at) - new Date(a.created_at),
      amount: (a, b) => b.transaction.amount - a.transaction.amount,
      confidence: (a, b) => (b.classifier_confidence ?? 0) - (a.classifier_confidence ?? 0),
    }

    return [...filtered].sort(comparators[sort])
  }, [exceptions, category, state, sort, search])

  const counts = useMemo(() => {
    const byCategory = Object.fromEntries(EXCEPTION_CATEGORIES.map((c) => [c, 0]))
    exceptions.forEach((item) => {
      byCategory[item.category] = (byCategory[item.category] ?? 0) + 1
    })
    return byCategory
  }, [exceptions])

  const openValue = rows.reduce((acc, item) => acc + item.transaction.amount, 0)

  return (
    <div className="glass-strong overflow-hidden">
      {/* Filter bar */}
      <div className="space-y-3 border-b border-outline-variant/50 px-5 py-4">
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-[13rem] flex-1">
            <Icon
              name="search"
              size={14}
              className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-on-surface-muted"
            />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search reference, counterparty or transaction ID…"
              aria-label="Search exceptions"
              className="w-full rounded-pill border border-outline-variant/70 bg-surface/70 py-2 pl-9 pr-4
                         text-[0.8125rem] font-medium text-on-surface backdrop-blur-glass
                         placeholder:text-on-surface-muted focus:border-primary focus:outline-none"
            />
          </div>

          <div className="segmented" role="group" aria-label="Filter by state">
            {STATE_FILTERS.map((value) => (
              <button
                key={value}
                type="button"
                data-active={state === value}
                onClick={() => setState(value)}
                className="segmented-item"
              >
                {value === 'ALL' ? 'All' : EXCEPTION_STATES[value].label}
              </button>
            ))}
          </div>

          <label className="flex items-center gap-2 text-caption text-on-surface-muted">
            <Icon name="filter" size={13} />
            <select
              value={sort}
              onChange={(e) => setSort(e.target.value)}
              aria-label="Sort exceptions"
              className="rounded-pill border border-outline-variant/70 bg-surface/70 px-3 py-1.5
                         text-caption text-on-surface focus:border-primary focus:outline-none"
            >
              {SORTS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        </div>

        {/* Category chips */}
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            data-active={category === 'ALL'}
            onClick={() => setCategory('ALL')}
            className={`chip transition-colors duration-200 ${
              category === 'ALL'
                ? 'border-on-surface bg-on-surface text-white'
                : 'border-outline-variant bg-surface/60 text-on-surface-variant hover:border-outline'
            }`}
          >
            All {exceptions.length}
          </button>
          {EXCEPTION_CATEGORIES.map((value) => {
            const meta = CATEGORY_META[value]
            const active = category === value
            return (
              <button
                key={value}
                type="button"
                onClick={() => setCategory(active ? 'ALL' : value)}
                className={`chip transition-all duration-200 ${
                  active ? meta.chip : 'border-outline-variant bg-surface/60 text-on-surface-variant hover:border-outline'
                }`}
              >
                <span className="h-1.5 w-1.5 rounded-full" style={{ background: meta.color }} />
                {meta.label} {counts[value] ?? 0}
              </button>
            )
          })}
        </div>
      </div>

      {/* Rows */}
      {loading ? (
        <div className="space-y-3 p-5">
          {Array.from({ length: 5 }, (_, i) => (
            <div key={i} className="skeleton h-16 w-full" />
          ))}
        </div>
      ) : rows.length === 0 ? (
        <div className="flex flex-col items-center px-6 py-16 text-center">
          <span className="flex h-12 w-12 items-center justify-center rounded-full bg-surface-dim text-on-surface-muted">
            <Icon name="check" size={20} strokeWidth={2} />
          </span>
          <p className="mt-4 text-title text-on-surface">Nothing in the queue</p>
          <p className="mt-1.5 max-w-sm text-[0.8125rem] text-on-surface-variant">
            No exceptions match these filters. Clear them to see the full queue, or wait for the
            next reconciliation pass.
          </p>
        </div>
      ) : (
        <ul>
          {rows.map((item) => (
            <Row
              key={item.id}
              item={item}
              expanded={expandedId === item.id}
              onToggle={() => setExpandedId(expandedId === item.id ? null : item.id)}
              canResolve={canResolve}
              busy={busyId === item.id}
              isNew={newIds.has(item.id)}
              onDecide={(decision) => onResolve(item, decision)}
            />
          ))}
        </ul>
      )}

      {/* Footer summary */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-outline-variant/50 px-5 py-3">
        <p className="text-[0.6875rem] font-medium text-on-surface-muted">
          Showing <span className="font-bold text-on-surface-variant">{rows.length}</span> of{' '}
          {exceptions.length} · exposure{' '}
          <span className="tabular font-bold text-on-surface-variant">
            {currency(openValue, 'USD', { compact: true })}
          </span>
        </p>
        <p className="flex items-center gap-1.5 text-[0.6875rem] font-medium text-on-surface-muted">
          <Icon name="sparkle" size={12} />
          Every decision is captured and feeds the retraining cycle
        </p>
      </div>
    </div>
  )
}

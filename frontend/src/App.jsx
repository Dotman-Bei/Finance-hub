import { useCallback, useEffect, useRef, useState } from 'react'

import ExceptionPanel from './components/ExceptionPanel'
import FloatingNav from './components/FloatingNav'
import Footer from './components/Footer'
import Hero from './components/Hero'
import KpiSummaryCards from './components/KpiSummaryCards'
import MatchRateChart from './components/MatchRateChart'
import ReportsPanel from './components/ReportsPanel'
import SignIn from './components/SignIn'
import ToastStack from './components/ToastStack'
import Icon from './components/ui/Icon'
import Section from './components/ui/Section'

import { auth } from './api/axiosClient'
import {
  connectionStatus,
  downloadReport,
  generateReport,
  getCategoryBreakdown,
  getExceptions,
  getKpi,
  getMatchRate,
  getReports,
  normalizeException,
  probePrincipal,
  resolveException,
  signIn,
} from './api/reportingApi'
import { ROLES } from './lib/constants'
import { currency, relativeTime } from './lib/format'
import useCursorGradient from './hooks/useCursorGradient'
import useToasts from './hooks/useToasts'
import useWebSocket from './hooks/useWebSocket'

/**
 * Failure banner.
 *
 * Shown when the gateway cannot be reached or a load failed. There is no
 * fallback data behind it: the panels below stay empty, because a dashboard
 * that fills itself with invented figures during an outage cannot be told
 * apart from one showing real ones.
 */
function ErrorBanner({ error, onRefresh, refreshing }) {
  if (!error) return null

  const offline = error.offline
  const forbidden = error.status === 401 || error.status === 403

  return (
    <div className="mx-auto w-full max-w-6xl px-6">
      <div className="glass flex flex-wrap items-center gap-3 border-[#F7CDCE] bg-[#FEF4F4]/80 px-4 py-3">
        <Icon name="alert" size={15} className="text-quarantined" />
        <div className="flex-1">
          <p className="text-[0.8125rem] font-bold text-[#8A2A2D]">
            {offline
              ? 'Reporting API unreachable'
              : forbidden
                ? 'Not authorised'
                : 'Could not load dashboard data'}
          </p>
          <p className="mt-0.5 text-[0.75rem] font-medium leading-snug text-[#A4595B]">
            {offline ? (
              <>
                No data is shown because none could be read. Start{' '}
                <code className="font-mono text-[0.6875rem]">reporting_api</code> on port 8000
                (<code className="font-mono text-[0.6875rem]">docker compose up -d reporting_api</code>).
              </>
            ) : (
              error.message
            )}
          </p>
        </div>
        <button type="button" onClick={onRefresh} disabled={refreshing} className="btn-ghost">
          <Icon name="refresh" size={13} className={refreshing ? 'animate-spin' : ''} />
          Retry
        </button>
      </div>
    </div>
  )
}

/**
 * Authentication state.
 *
 * `checking` is a real state, not a loading flicker: until the gateway has been
 * asked whether it enforces auth at all, showing either the dashboard or the
 * sign-in card would be a guess. `REQUIRE_AUTH=false` deployments answer
 * /auth/me without credentials and skip the card entirely.
 */
const AUTH_CHECKING = 'checking'
const AUTH_REQUIRED = 'required'
const AUTH_READY = 'ready'

export default function App() {
  const [role, setRole] = useState(() => auth.getRole())
  const [authState, setAuthState] = useState(
    // A stored token is assumed good until a 401 says otherwise. Re-probing on
    // every mount would cost a round trip to learn what the next request
    // reveals anyway.
    () => (auth.getToken() ? AUTH_READY : AUTH_CHECKING)
  )
  const [authError, setAuthError] = useState(null)
  const [authPending, setAuthPending] = useState(false)
  const [connection, setConnection] = useState(connectionStatus.get())

  const [kpi, setKpi] = useState(null)
  const [series, setSeries] = useState([])
  const [exceptions, setExceptions] = useState([])
  const [reports, setReports] = useState([])
  const [categories, setCategories] = useState([])

  const [loadError, setLoadError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [running, setRunning] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [busyExceptionId, setBusyExceptionId] = useState(null)
  const [downloadingId, setDownloadingId] = useState(null)
  const [newIds, setNewIds] = useState(() => new Set())

  const { toasts, push, dismiss } = useToasts()
  const rootRef = useRef(null)
  useCursorGradient(rootRef)

  const permissions = ROLES[role].can

  useEffect(() => connectionStatus.subscribe(setConnection), [])

  useEffect(() => {
    auth.setRole(role)
  }, [role])

  /* ── Authentication ─────────────────────────────────────────────────── */

  useEffect(() => {
    if (authState !== AUTH_CHECKING) return

    let cancelled = false
    probePrincipal()
      .then((principal) => {
        if (cancelled) return
        if (principal) {
          // The gateway answered without a token, so auth is disabled here.
          if (principal.role) setRole(principal.role)
          setAuthState(AUTH_READY)
        } else {
          setAuthState(AUTH_REQUIRED)
        }
      })
      .catch(() => {
        // The gateway is unreachable rather than refusing us. Show the card:
        // it carries the "start the gateway" message and a way to retry.
        if (!cancelled) setAuthState(AUTH_REQUIRED)
      })

    return () => {
      cancelled = true
    }
  }, [authState])

  const handleSignIn = useCallback(async ({ role: nextRole, apiKey }) => {
    setAuthPending(true)
    setAuthError(null)
    try {
      const data = await signIn({ role: nextRole, apiKey })
      auth.setToken(data.access_token)
      // Remember the key so a role switch can mint a new token without asking
      // for it again. sessionStorage, not localStorage: it should not outlive
      // the tab the way the token deliberately does.
      sessionStorage.setItem('financehub.key', apiKey)
      setRole(nextRole)
      setAuthState(AUTH_READY)
    } catch (error) {
      setAuthError(error)
    } finally {
      setAuthPending(false)
    }
  }, [])

  const handleSignOut = useCallback(() => {
    auth.setToken('')
    sessionStorage.removeItem('financehub.key')
    setAuthError(null)
    setAuthState(AUTH_REQUIRED)
  }, [])

  // `handleRoleChange` needs `load`, so it lives with the other actions below.

  /* ── Data loading ───────────────────────────────────────────────────── */

  const load = useCallback(
    async ({ silent = false } = {}) => {
      if (!silent) setLoading(true)
      try {
        // allSettled, not all: one failing panel must not blank the other
        // three. Each is either real data or visibly empty.
        const [kpiRes, seriesRes, exceptionsRes, reportsRes, categoriesRes] =
          await Promise.allSettled([
            getKpi(),
            getMatchRate(),
            getExceptions(),
            getReports(),
            getCategoryBreakdown(),
          ])

        if (kpiRes.status === 'fulfilled') setKpi(kpiRes.value)
        if (seriesRes.status === 'fulfilled') setSeries(seriesRes.value)
        if (exceptionsRes.status === 'fulfilled') setExceptions(exceptionsRes.value)
        if (reportsRes.status === 'fulfilled') setReports(reportsRes.value)
        if (categoriesRes.status === 'fulfilled') setCategories(categoriesRes.value)

        const failure = [kpiRes, seriesRes, exceptionsRes, reportsRes, categoriesRes].find(
          (r) => r.status === 'rejected'
        )
        setLoadError(failure ? failure.reason : null)

        if (failure && !silent) {
          push({
            tone: 'error',
            title: 'Could not load dashboard data',
            body: failure.reason?.message ?? 'Unknown error',
          })
        }
      } finally {
        setLoading(false)
      }
    },
    [push]
  )

  useEffect(() => {
    if (authState === AUTH_READY) load()
  }, [load, authState])

  // A token that expires mid-session, or one minted before the gateway's secret
  // changed, surfaces as 401 on every panel. Returning to the card is the only
  // action that can fix it; leaving four error cards up is not.
  useEffect(() => {
    if (loadError?.status === 401) handleSignOut()
  }, [loadError, handleSignOut])

  /* ── Live exception feed ────────────────────────────────────────────── */

  const onSocketMessage = useCallback(
    (message) => {
      if (message?.type !== 'exception.created' || !message.payload) return

      const item = normalizeException(message.payload)
      setExceptions((current) => [item, ...current].slice(0, 200))
      setNewIds((current) => new Set(current).add(item.id))

      push({
        tone: 'warning',
        title: 'New exception queued',
        body: `${currency(item.transaction.amount, item.transaction.currency)} — ${item.transaction.description}`,
        action: {
          label: 'Open exception queue',
          onClick: () =>
            document.getElementById('exceptions')?.scrollIntoView({ behavior: 'smooth' }),
        },
      })
    },
    [push]
  )

  const socket = useWebSocket('/ws/exceptions', { onMessage: onSocketMessage })

  /* ── Actions ────────────────────────────────────────────────────────── */

  const handleRun = useCallback(async () => {
    setRunning(true)
    push({ tone: 'info', title: 'Reconciliation pass requested', body: 'Refreshing every panel…' })
    await load({ silent: true })
    setRunning(false)
    push({ tone: 'success', title: 'Dashboard up to date', body: 'Metrics re-read from the gateway.' })
  }, [load, push])

  /**
   * Switching role re-issues the token.
   *
   * The role claim lives inside the signature; the `X-FinanceHub-Role` header
   * is explicitly untrusted by the gateway. Without re-issuing, changing role
   * would only change which buttons the UI draws while the server went on
   * applying the old permissions — the appearance of RBAC rather than RBAC.
   */
  const handleRoleChange = useCallback(
    async (nextRole) => {
      const apiKey = sessionStorage.getItem('financehub.key')
      if (!auth.getToken() || !apiKey) {
        // Nothing to re-sign with: auth is disabled, or the key was never
        // captured. UI-level scoping still applies.
        setRole(nextRole)
        return
      }

      const previous = role
      setRole(nextRole)
      try {
        const data = await signIn({ role: nextRole, apiKey })
        auth.setToken(data.access_token)
        await load({ silent: true })
        push({
          tone: 'info',
          title: `Viewing as ${ROLES[nextRole].label}`,
          body: 'A new token was issued; the gateway now enforces this role.',
        })
      } catch (error) {
        setRole(previous)
        push({ tone: 'error', title: 'Could not switch role', body: error.message })
      }
    },
    [role, load, push]
  )

  const handleResolve = useCallback(
    async (item, decision) => {
      setBusyExceptionId(item.id)
      try {
        const data = await resolveException(item.id, decision)
        setExceptions((current) =>
          current.map((row) =>
            row.id === item.id
              ? {
                  ...row,
                  state: data.state,
                  resolved_at: data.resolved_at,
                  resolved_by: 'you',
                  suggested_resolution: data.resolution ?? row.suggested_resolution,
                }
              : row
          )
        )
        setKpi((current) =>
          current
            ? { ...current, open_exceptions: Math.max(0, current.open_exceptions - 1) }
            : current
        )
        push({
          tone: decision.decision === 'REJECT' ? 'info' : 'success',
          title: decision.decision === 'REJECT' ? 'Suggestion rejected' : 'Exception resolved',
          body: 'Recorded in the audit trail and added to the classifier training set.',
        })
      } catch (error) {
        push({ tone: 'error', title: 'Could not record the decision', body: error.message })
      } finally {
        setBusyExceptionId(null)
      }
    },
    [push]
  )

  const handleGenerate = useCallback(
    async (request) => {
      setGenerating(true)
      try {
        const data = await generateReport(request)
        setReports((current) => [data, ...current])
        push({
          tone: 'success',
          title: 'Report ready',
          body: `${data.name} stored under ID ${data.id.slice(0, 8).toUpperCase()}.`,
          action: { label: 'Download PDF', onClick: () => downloadReport(data) },
        })
      } catch (error) {
        push({ tone: 'error', title: 'Report generation failed', body: error.message })
      } finally {
        setGenerating(false)
      }
    },
    [push]
  )

  const handleDownload = useCallback(
    async (report) => {
      setDownloadingId(report.id)
      try {
        await downloadReport(report)
      } catch (error) {
        push({ tone: 'error', title: 'Download failed', body: error.message })
      } finally {
        setDownloadingId(null)
      }
    },
    [push]
  )

  const scrollToReports = useCallback(() => {
    document.getElementById('reports')?.scrollIntoView({ behavior: 'smooth' })
  }, [])

  /* ── Render ─────────────────────────────────────────────────────────── */

  if (authState === AUTH_CHECKING) {
    // Blank rather than a spinner: this resolves in one round trip, and a
    // flashed loader is more distracting than a beat of nothing.
    return <div className="min-h-screen bg-surface" />
  }

  if (authState === AUTH_REQUIRED) {
    return <SignIn onSignIn={handleSignIn} pending={authPending} error={authError} />
  }

  return (
    <div ref={rootRef} className="relative min-h-screen bg-surface">
      <FloatingNav
        role={role}
        onRoleChange={handleRoleChange}
        onRun={handleRun}
        running={running}
        canRun={permissions.runReconciliation}
        // Omitted when no token is in play: a REQUIRE_AUTH=false stack has
        // nothing to sign out of, and offering it would strand the user at a
        // card they cannot get past.
        onSignOut={auth.getToken() ? handleSignOut : undefined}
      />

      <main>
        <Hero kpi={kpi} live={connection.live} feedLive={socket.isLive} />

        <div className="mx-auto w-full max-w-6xl space-y-rhythm-2 px-6">
          <ErrorBanner
            error={loadError}
            onRefresh={() => load({ silent: true })}
            refreshing={loading}
          />

          <Section
            id="overview"
            eyebrow="Overview"
            title="On-demand reconciliation visibility"
            description="The four figures finance leadership asks for, recomputed on every pass and cached between polls."
            action={
              <p className="flex items-center gap-2 text-[0.75rem] font-semibold text-on-surface-muted">
                <span
                  className={`h-1.5 w-1.5 rounded-full ${
                    socket.isLive ? 'bg-matched' : 'bg-exception'
                  }`}
                />
                {/* Socket open and events flowing are different facts; a
                    connected socket with a dead upstream bus is not live. */}
                {socket.isLive
                  ? 'Live feed'
                  : socket.isConnected
                    ? 'Connected · event bus down'
                    : 'Feed disconnected'}{' '}
                · last pass {relativeTime(kpi?.last_run_at)}
              </p>
            }
          >
            <KpiSummaryCards kpi={kpi} series={series} loading={loading} />
          </Section>

          <Section
            id="analytics"
            layout="split"
            keyline
            eyebrow="Analytics"
            title="Where the match rate comes from"
            description="Deterministic rules clear the bulk of the volume; the unsupervised layer works the remainder above a configurable confidence floor. Everything below it becomes an exception."
          >
            <MatchRateChart series={series} categories={categories} loading={loading} />
          </Section>

          <Section
            id="exceptions"
            eyebrow="Exception queue"
            title="Every unmatched item, classified and explained"
            description="Four categories, each with a suggested remediation pathway. Accept, amend or reject — the decision is logged and feeds the next retraining round."
          >
            <ExceptionPanel
              exceptions={exceptions}
              loading={loading}
              canResolve={permissions.resolveExceptions}
              busyId={busyExceptionId}
              newIds={newIds}
              onResolve={handleResolve}
            />
          </Section>

          <Section
            id="reports"
            eyebrow="Reports"
            title="Audit-ready by default"
            description="Generate a PDF covering reconciliation summaries, exception logs and match-rate analytics. Each one is persisted with its ID."
          >
            <ReportsPanel
              reports={reports}
              loading={loading}
              canGenerate={permissions.generateReports}
              generating={generating}
              downloadingId={downloadingId}
              onGenerate={handleGenerate}
              onDownload={handleDownload}
            />
          </Section>
        </div>
      </main>

      <Footer onGenerateReport={scrollToReports} canGenerate={permissions.generateReports} />

      <ToastStack toasts={toasts} onDismiss={dismiss} />
    </div>
  )
}

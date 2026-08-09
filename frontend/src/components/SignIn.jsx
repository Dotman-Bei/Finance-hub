import { useEffect, useState } from 'react'

import Icon from './ui/Icon'
import Logo from './ui/Logo'
import { ROLES } from '../lib/constants'

/**
 * Sign-in gate for the dashboard (RBAC, §3.4.1).
 *
 * Every data endpoint on the gateway is permission-guarded, so without a token
 * the dashboard renders four panels of 401s. This is what obtains one.
 *
 * It is deliberately not a login form: `POST /auth/token` verifies a single
 * shared secret and mints a token for whichever role is asked for — there is
 * no user store in this system, and pretending otherwise with a
 * username/password box would misrepresent what is actually being checked. A
 * real deployment puts an identity provider in front of the gateway and drops
 * the endpoint entirely.
 *
 * The key prefills from VITE_SERVICE_API_KEY so a demo is one click. That
 * variable is compiled into the public bundle and is readable by anyone with
 * devtools, which is acceptable for a shared demo secret and would not be for
 * anything else.
 */
export default function SignIn({ onSignIn, pending, error }) {
  const [role, setRole] = useState('FINANCE_MANAGER')
  const [apiKey, setApiKey] = useState(import.meta.env.VITE_SERVICE_API_KEY ?? '')
  const [touched, setTouched] = useState(false)

  // A rejected key is the one failure the field itself can fix, so surface it
  // there and let the user correct it in place.
  useEffect(() => {
    if (error) setTouched(false)
  }, [error])

  const prefilled = Boolean(import.meta.env.VITE_SERVICE_API_KEY)
  const canSubmit = apiKey.trim().length > 0 && !pending

  const submit = (event) => {
    event.preventDefault()
    setTouched(true)
    if (!canSubmit) return
    onSignIn({ role, apiKey: apiKey.trim() })
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center bg-surface px-6 py-16">
      <form onSubmit={submit} className="glass w-full max-w-md px-7 py-8">
        <Logo size={24} />

        <p className="eyebrow mt-6">Sign in</p>
        <h1 className="mt-1 text-[1.375rem] font-bold leading-tight text-on-surface">
          Choose the role to view as
        </h1>
        <p className="mt-2 text-[0.8125rem] font-medium leading-snug text-on-surface-muted">
          The gateway enforces permissions from the role inside the signed token,
          not from anything the browser claims afterwards.
        </p>

        <fieldset className="mt-6 space-y-2" disabled={pending}>
          <legend className="sr-only">Role</legend>
          {Object.entries(ROLES).map(([key, meta]) => (
            <label
              key={key}
              className={`flex cursor-pointer items-start gap-3 rounded-xl border px-3.5 py-3 transition ${
                role === key
                  ? 'border-accent bg-accent/[0.06]'
                  : 'border-hairline hover:border-on-surface-muted/40'
              }`}
            >
              <input
                type="radio"
                name="role"
                value={key}
                checked={role === key}
                onChange={() => setRole(key)}
                className="mt-1 accent-accent"
              />
              <span className="flex-1">
                <span className="block text-[0.8125rem] font-bold text-on-surface">
                  {meta.label}
                </span>
                <span className="mt-0.5 block text-[0.75rem] font-medium leading-snug text-on-surface-muted">
                  {meta.blurb}
                </span>
              </span>
            </label>
          ))}
        </fieldset>

        <label className="mt-5 block">
          <span className="eyebrow">Service API key</span>
          <input
            type="password"
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
            disabled={pending}
            autoComplete="off"
            placeholder="SERVICE_API_KEY"
            className="mt-1.5 w-full rounded-xl border border-hairline bg-white/70 px-3.5 py-2.5 font-mono text-[0.8125rem] text-on-surface outline-none transition focus:border-accent"
          />
          <span className="mt-1.5 block text-[0.6875rem] font-medium text-on-surface-muted">
            {prefilled
              ? 'Prefilled from VITE_SERVICE_API_KEY.'
              : 'Matches SERVICE_API_KEY on the reporting gateway.'}
          </span>
        </label>

        {touched && !apiKey.trim() && (
          <p className="mt-3 text-[0.75rem] font-semibold text-quarantined">
            Enter the service API key to continue.
          </p>
        )}

        {error && (
          <div className="mt-4 flex items-start gap-2.5 rounded-xl border border-[#F7CDCE] bg-[#FEF4F4]/80 px-3.5 py-3">
            <Icon name="alert" size={14} className="mt-0.5 text-quarantined" />
            <div>
              <p className="text-[0.75rem] font-bold text-[#8A2A2D]">
                {error.offline ? 'Reporting API unreachable' : 'Could not sign in'}
              </p>
              <p className="mt-0.5 text-[0.6875rem] font-medium leading-snug text-[#A4595B]">
                {error.offline
                  ? 'Start the gateway on port 8000, then try again.'
                  : error.message}
              </p>
            </div>
          </div>
        )}

        <button type="submit" disabled={!canSubmit} className="btn-primary mt-6 w-full justify-center">
          {pending ? (
            <>
              <Icon name="refresh" size={14} className="animate-spin" />
              Signing in…
            </>
          ) : (
            <>
              <Icon name="shield" size={14} />
              Sign in as {ROLES[role].short}
            </>
          )}
        </button>
      </form>
    </div>
  )
}

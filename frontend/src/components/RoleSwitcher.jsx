import { useEffect, useRef, useState } from 'react'
import Icon from './ui/Icon'
import { ROLES } from '../lib/constants'

/**
 * Active-role selector (RBAC, §3.4.1).
 *
 * The gateway is the real enforcement point — this switches the *view* and the
 * affordances the UI offers, and the chosen role rides along on every request
 * as `X-FinanceHub-Role`.
 */
export default function RoleSwitcher({ role, onChange }) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef(null)
  const current = ROLES[role]

  useEffect(() => {
    if (!open) return undefined

    const onPointerDown = (event) => {
      if (!rootRef.current?.contains(event.target)) setOpen(false)
    }
    const onKeyDown = (event) => {
      if (event.key === 'Escape') setOpen(false)
    }

    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="flex items-center gap-2 rounded-pill px-3 py-2 text-caption text-on-surface-variant
                   transition-colors duration-200 hover:bg-surface-dim/80 hover:text-on-surface"
      >
        <Icon name="user" size={14} />
        <span className="hidden sm:inline">{current.short}</span>
        <Icon
          name="chevron"
          size={13}
          className={`transition-transform duration-300 ${open ? 'rotate-180' : ''}`}
        />
      </button>

      {open && (
        <div
          role="listbox"
          className="glass-strong absolute right-0 top-[calc(100%+10px)] z-50 w-72 origin-top-right
                     animate-fade-up overflow-hidden p-1.5"
        >
          <p className="eyebrow px-3 pb-1.5 pt-2">Active role</p>
          {Object.entries(ROLES).map(([key, meta]) => {
            const selected = key === role
            return (
              <button
                key={key}
                type="button"
                role="option"
                aria-selected={selected}
                onClick={() => {
                  onChange(key)
                  setOpen(false)
                }}
                className={`flex w-full items-start gap-3 rounded-2xl px-3 py-2.5 text-left transition-colors duration-200 ${
                  selected ? 'bg-primary-50' : 'hover:bg-surface-dim/80'
                }`}
              >
                <span
                  className={`mt-1 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border ${
                    selected ? 'border-primary bg-primary text-white' : 'border-outline'
                  }`}
                >
                  {selected && <Icon name="check" size={9} strokeWidth={3} />}
                </span>
                <span className="min-w-0">
                  <span className="block text-[0.8125rem] font-bold tracking-tight-ui text-on-surface">
                    {meta.label}
                  </span>
                  <span className="mt-0.5 block text-[0.75rem] leading-snug text-on-surface-variant">
                    {meta.blurb}
                  </span>
                </span>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

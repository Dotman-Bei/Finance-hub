import Icon from './ui/Icon'

const TONE = {
  info: { icon: 'bell', accent: 'text-electric', ring: 'border-[#BFDBFE]' },
  success: { icon: 'check', accent: 'text-matched', ring: 'border-[#BFE9E2]' },
  warning: { icon: 'alert', accent: 'text-exception', ring: 'border-[#FDE6C7]' },
  error: { icon: 'close', accent: 'text-quarantined', ring: 'border-[#F7CDCE]' },
}

/**
 * Live notification stack — driven by `WS /ws/exceptions` and by the outcome of
 * user actions, so teams don't have to refresh to see new work arrive.
 */
export default function ToastStack({ toasts, onDismiss }) {
  if (!toasts.length) return null

  return (
    <div
      className="pointer-events-none fixed bottom-5 right-5 z-[60] flex w-[min(22rem,calc(100vw-2.5rem))] flex-col gap-2.5"
      role="status"
      aria-live="polite"
    >
      {toasts.map((toast) => {
        const tone = TONE[toast.tone] ?? TONE.info
        return (
          <article
            key={toast.id}
            className={`glass-strong pointer-events-auto flex animate-slide-in-right items-start gap-3 border p-3.5 ${tone.ring}`}
          >
            <span
              className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-surface
                          shadow-[0_1px_3px_rgba(16,16,20,0.1)] ${tone.accent}`}
            >
              <Icon name={tone.icon} size={13} strokeWidth={2.2} />
            </span>

            <div className="min-w-0 flex-1">
              <p className="text-[0.8125rem] font-bold tracking-tight-ui text-on-surface">
                {toast.title}
              </p>
              {toast.body && (
                <p className="mt-0.5 text-[0.75rem] leading-snug text-on-surface-variant">
                  {toast.body}
                </p>
              )}
              {toast.action && (
                <button type="button" onClick={toast.action.onClick} className="btn-quiet mt-2 px-0">
                  {toast.action.label}
                  <Icon name="arrowUpRight" size={12} />
                </button>
              )}
            </div>

            <button
              type="button"
              onClick={() => onDismiss(toast.id)}
              aria-label="Dismiss notification"
              className="-mr-1 -mt-1 rounded-full p-1.5 text-on-surface-muted transition-colors
                         duration-200 hover:bg-surface-dim hover:text-on-surface"
            >
              <Icon name="close" size={12} strokeWidth={2.2} />
            </button>
          </article>
        )
      })}
    </div>
  )
}

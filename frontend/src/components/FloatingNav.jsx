import Icon from './ui/Icon'
import Logo from './ui/Logo'
import RoleSwitcher from './RoleSwitcher'
import useScrollSpy from '../hooks/useScrollSpy'

const LINKS = [
  { id: 'overview', label: 'Overview' },
  { id: 'analytics', label: 'Analytics' },
  { id: 'exceptions', label: 'Exceptions' },
  { id: 'reports', label: 'Reports' },
]

/**
 * Floating pill navigation — centred, hugging its content, glass over whatever
 * scrolls beneath it. Both gutters are 8px with an inset element at each end,
 * so the left (logo) and right (CTA) paddings mirror one another exactly.
 */
export default function FloatingNav({ role, onRoleChange, onRun, running, canRun }) {
  const active = useScrollSpy(LINKS.map((l) => l.id))

  const scrollTo = (id) => (event) => {
    event.preventDefault()
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  return (
    <header className="pointer-events-none fixed inset-x-0 top-4 z-50 flex justify-center px-4">
      <nav
        aria-label="Primary"
        className="pointer-events-auto flex w-full max-w-4xl items-center justify-between gap-2
                   rounded-pill border border-outline-variant/30 bg-surface/80 p-2 shadow-float
                   backdrop-blur-glass-xl"
      >
        {/* Left gutter — logo */}
        <a
          href="#overview"
          onClick={scrollTo('overview')}
          className="flex shrink-0 items-center rounded-pill px-3 py-1.5 transition-opacity duration-200 hover:opacity-70"
        >
          <Logo size={24} className="hidden sm:inline-flex" />
          <Logo size={24} showWordmark={false} className="sm:hidden" />
        </a>

        {/* Centre — section links */}
        <ul className="hidden items-center gap-0.5 md:flex">
          {LINKS.map((link) => (
            <li key={link.id}>
              <a
                href={`#${link.id}`}
                onClick={scrollTo(link.id)}
                aria-current={active === link.id ? 'page' : undefined}
                className={`relative rounded-pill px-3.5 py-2 text-caption transition-colors duration-300 ${
                  active === link.id
                    ? 'text-on-surface'
                    : 'text-on-surface-variant hover:text-on-surface'
                }`}
              >
                {link.label}
                <span
                  className={`absolute inset-x-3.5 -bottom-0.5 h-[2px] rounded-full bg-grad-warm transition-all duration-300 ${
                    active === link.id ? 'opacity-100' : 'scale-x-0 opacity-0'
                  }`}
                />
              </a>
            </li>
          ))}
        </ul>

        {/* Right gutter — role + CTA */}
        <div className="flex shrink-0 items-center gap-1">
          <RoleSwitcher role={role} onChange={onRoleChange} />
          <button
            type="button"
            onClick={onRun}
            disabled={!canRun || running}
            title={canRun ? 'Run a reconciliation pass' : 'This role has read-only access'}
            className="btn-primary px-4 py-2"
          >
            <Icon
              name={running ? 'refresh' : 'play'}
              size={13}
              className={running ? 'animate-spin' : ''}
            />
            <span className="hidden sm:inline">{running ? 'Reconciling…' : 'Reconcile'}</span>
          </button>
        </div>
      </nav>
    </header>
  )
}

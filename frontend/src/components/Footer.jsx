import Icon from './ui/Icon'
import Logo from './ui/Logo'

const COLUMNS = [
  {
    title: 'Subsystems',
    links: ['Matching Engine', 'Validation Pipeline', 'Exception Handling', 'Reporting'],
  },
  { title: 'Reporting', links: ['KPI Overview', 'Match-Rate Analytics', 'Audit Trail', 'Exports'] },
  { title: 'Governance', links: ['Role Access', 'Data Retention', 'Compliance', 'Status'] },
]

/**
 * Footer — pale cursor-reactive wash, a single restrained CTA centred above the
 * link columns, and a symmetrical base row: mark bottom-left, copyright
 * bottom-right, both solid black. Paints to the viewport edge with no seam.
 */
export default function Footer({ onGenerateReport, canGenerate }) {
  return (
    <footer className="relative isolate mt-rhythm-2 overflow-hidden bg-surface-variant">
      {/* Pale gradient that drifts with the pointer */}
      <div className="cursor-aura pointer-events-none absolute inset-0 -z-10" aria-hidden="true" />
      <div
        className="pointer-events-none absolute inset-x-0 top-0 -z-10 h-px bg-gradient-to-r
                   from-transparent via-outline-variant to-transparent"
        aria-hidden="true"
      />

      <div className="mx-auto w-full max-w-6xl px-6 pb-8 pt-rhythm-2">
        {/* Centred CTA — restrained size, deliberately not full-width */}
        <div className="flex flex-col items-center text-center">
          <p className="eyebrow">Close the period</p>
          <h2 className="mt-3 max-w-xl text-headline">
            Every transaction, <span className="text-gradient-energy">reconciled and evidenced</span>.
          </h2>
          <p className="mx-auto mt-3 max-w-md text-[0.875rem] leading-relaxed text-on-surface-variant">
            Export an audit-ready package covering reconciliation summaries, exception logs and
            match-rate analytics.
          </p>
          <button
            type="button"
            onClick={onGenerateReport}
            disabled={!canGenerate}
            className="btn-primary mt-6"
          >
            <Icon name="document" size={13} />
            Generate audit report
          </button>
        </div>

        {/* Link columns */}
        <div className="mt-rhythm-2 grid grid-cols-2 gap-8 sm:grid-cols-3">
          {COLUMNS.map((column) => (
            <div key={column.title}>
              <p className="eyebrow">{column.title}</p>
              <ul className="mt-4 space-y-2.5">
                {column.links.map((link) => (
                  <li key={link}>
                    <a
                      href="#overview"
                      className="text-[0.8125rem] font-medium text-on-surface-variant
                                 transition-colors duration-200 hover:text-on-surface"
                    >
                      {link}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>

        {/* Symmetrical base row — black mark left, black copyright right */}
        <div
          className="mt-rhythm-2 flex flex-col items-center gap-4 border-t border-outline-variant/60
                     pt-8 sm:flex-row sm:justify-between"
        >
          <Logo size={22} />
          <p className="text-[0.75rem] font-bold tracking-tight-ui text-on-surface">
            © {new Date().getFullYear()} FinanceHub. All rights reserved.
          </p>
        </div>
      </div>
    </footer>
  )
}

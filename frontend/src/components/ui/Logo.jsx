/**
 * FinanceHub keyline mark.
 *
 * Two ledger strokes that converge on a single node — the reconciliation idea,
 * drawn as a hairline glyph. Always solid black for contrast (brand rule);
 * `tone="light"` is reserved for placement on dark or gradient fills.
 */

export function LogoMark({ size = 26, tone = 'dark', className = '' }) {
  const stroke = tone === 'light' ? '#FFFFFF' : '#000000'

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      aria-hidden="true"
      className={className}
    >
      <rect x="1" y="1" width="30" height="30" rx="9" stroke={stroke} strokeWidth="1.6" />
      {/* Two sources converging on one reconciled node */}
      <path
        d="M9 10.5h7.5a5.5 5.5 0 0 1 0 11H9"
        stroke={stroke}
        strokeWidth="1.9"
        strokeLinecap="round"
      />
      <path d="M9 16h5.5" stroke={stroke} strokeWidth="1.9" strokeLinecap="round" />
      <circle cx="22.4" cy="16" r="2.1" fill="#FF8A65" />
    </svg>
  )
}

export default function Logo({ size = 26, tone = 'dark', showWordmark = true, className = '' }) {
  return (
    <span className={`inline-flex items-center gap-2.5 ${className}`}>
      <LogoMark size={size} tone={tone} />
      {showWordmark && (
        <span
          className={`text-[0.9375rem] font-extrabold tracking-tighter ${
            tone === 'light' ? 'text-white' : 'text-on-surface'
          }`}
        >
          FinanceHub
        </span>
      )}
    </span>
  )
}

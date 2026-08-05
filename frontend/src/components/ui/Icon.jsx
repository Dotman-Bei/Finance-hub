/**
 * Inline stroke-icon set. Keyline weight (1.6) matches the brand's minimal,
 * hairline aesthetic; no icon-font or SVG-sprite dependency.
 */

const PATHS = {
  pulse: 'M3 12h3.5l2.5-7 4 14 2.5-7H21',
  alert: 'M12 9v4m0 3.5v.01M10.3 3.9 2.4 17.5A2 2 0 0 0 4.1 20.5h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z',
  document: 'M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8m-5-5 5 5m-5-5v5h5M9 13h6M9 17h4',
  download: 'M12 3v12m0 0 4.5-4.5M12 15l-4.5-4.5M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2',
  check: 'm5 13 4.5 4.5L19 7',
  close: 'M6 6l12 12M18 6 6 18',
  pencil: 'M4 20h4L19.5 8.5a2.1 2.1 0 0 0-3-3L5 17v3ZM15 6l3 3',
  search: 'M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm6-2 4 4',
  filter: 'M3 5h18l-7 8v6l-4 2v-8L3 5Z',
  chevron: 'm6 9 6 6 6-6',
  arrowUpRight: 'M7 17 17 7m0 0H8m9 0v9',
  trendUp: 'M3 17l6-6 4 4 8-8m0 0h-6m6 0v6',
  trendDown: 'M3 7l6 6 4-4 8 8m0 0h-6m6 0v-6',
  refresh: 'M20 11A8 8 0 0 0 6.3 6.3L4 8.5M4 4v4.5h4.5M4 13a8 8 0 0 0 13.7 4.7L20 15.5M20 20v-4.5h-4.5',
  shield: 'M12 3 4.5 6v6c0 4.4 3.1 8.1 7.5 9 4.4-.9 7.5-4.6 7.5-9V6L12 3Zm-2.5 9 2 2 4-4.5',
  clock: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-13.5V12l3 2',
  layers: 'm12 3 9 5-9 5-9-5 9-5Zm9 9-9 5-9-5m18 4.5-9 5-9-5',
  bell: 'M18 8.5a6 6 0 1 0-12 0c0 5-2 6.5-2 6.5h16s-2-1.5-2-6.5ZM10.3 19a2 2 0 0 0 3.4 0',
  sparkle: 'M12 3.5 13.7 9l5.5 1.7-5.5 1.8L12 18l-1.7-5.5L4.8 10.7 10.3 9 12 3.5ZM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8L19 16Z',
  scale: 'M12 3v18M7 21h10M12 6 5 9m7-3 7 3M5 9l-2.5 5a3.2 3.2 0 0 0 5 0L5 9Zm14 0-2.5 5a3.2 3.2 0 0 0 5 0L19 9Z',
  wallet: 'M3 8a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2m-16 0v9a2 2 0 0 0 2 2h13a2 2 0 0 0 2-2v-3m-4 0h5v-3h-5a1.5 1.5 0 0 0 0 3Z',
  play: 'M7 5.5v13l11-6.5-11-6.5Z',
  grid: 'M4 4h7v7H4V4Zm9 0h7v7h-7V4ZM4 13h7v7H4v-7Zm9 0h7v7h-7v-7Z',
  user: 'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm-8 8a8 8 0 0 1 16 0',
  plus: 'M12 5v14M5 12h14',
  external: 'M14 4h6v6M20 4l-9 9M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4',
  history: 'M3 12a9 9 0 1 0 3-6.7L3 8m0-4.5V8h4.5M12 7.5V12l3.2 1.9',
}

export default function Icon({ name, size = 16, className = '', strokeWidth = 1.6, ...rest }) {
  const d = PATHS[name]
  if (!d) return null

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      className={className}
      {...rest}
    >
      <path d={d} />
    </svg>
  )
}

export const ICON_NAMES = Object.keys(PATHS)

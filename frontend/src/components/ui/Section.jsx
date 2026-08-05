import { LogoMark } from './Logo'

/**
 * Section shell.
 *
 * `split` puts the description in a left column whose first line sits flush
 * with the top of the content beside it, with the keyline mark anchored at the
 * bottom-left of the header. `stacked` runs the header full width above.
 */
export default function Section({
  id,
  eyebrow,
  title,
  description,
  action,
  layout = 'stacked',
  keyline = false,
  children,
}) {
  const header = (
    <div className={layout === 'split' ? 'flex h-full flex-col' : ''}>
      {eyebrow && <p className="eyebrow">{eyebrow}</p>}
      <h2 className="mt-3 text-headline">{title}</h2>
      {description && (
        <p className="mt-3 max-w-md text-[0.9375rem] leading-relaxed text-on-surface-variant">
          {description}
        </p>
      )}
      {keyline && layout === 'split' && (
        <div className="mt-auto hidden pt-10 lg:block">
          <LogoMark size={26} />
        </div>
      )}
    </div>
  )

  return (
    <section id={id} className="scroll-mt-28">
      {layout === 'split' ? (
        <div className="grid items-start gap-rhythm lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
          {header}
          <div className="min-w-0">{children}</div>
        </div>
      ) : (
        <>
          <div className="flex flex-wrap items-end justify-between gap-4">
            {header}
            {action}
          </div>
          <div className="mt-rhythm min-w-0">{children}</div>
        </>
      )}
    </section>
  )
}

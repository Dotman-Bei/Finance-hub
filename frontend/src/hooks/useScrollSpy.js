import { useEffect, useState } from 'react'

/**
 * Tracks which section id is currently in the reading zone, for nav highlighting.
 * The top offset clears the floating nav so a section isn't "active" while it
 * still sits behind the pill.
 */
export default function useScrollSpy(ids, { topOffset = 140 } = {}) {
  const [active, setActive] = useState(ids[0])
  const key = ids.join('|')

  useEffect(() => {
    const sections = key
      .split('|')
      .map((id) => document.getElementById(id))
      .filter(Boolean)

    if (!sections.length) return undefined

    const pickActive = () => {
      let current = sections[0]
      for (const section of sections) {
        if (section.getBoundingClientRect().top - topOffset <= 0) current = section
      }
      // At the very bottom the last section may never cross the line.
      const atBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 8
      setActive(atBottom ? sections[sections.length - 1].id : current.id)
    }

    pickActive()
    window.addEventListener('scroll', pickActive, { passive: true })
    window.addEventListener('resize', pickActive)
    return () => {
      window.removeEventListener('scroll', pickActive)
      window.removeEventListener('resize', pickActive)
    }
  }, [key, topOffset])

  return active
}

import { useEffect } from 'react'

/**
 * Publishes the pointer position onto `--cursor-x` / `--cursor-y` so the pale
 * background washes (.cursor-aura) drift with the mouse. Writes are coalesced
 * into one rAF per frame — no layout thrash, no per-move React renders.
 */
export default function useCursorGradient(targetRef) {
  useEffect(() => {
    const node = targetRef?.current ?? document.documentElement
    if (window.matchMedia('(pointer: coarse)').matches) return undefined
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return undefined

    let frame = 0
    let pending = null

    const flush = () => {
      frame = 0
      if (!pending) return
      node.style.setProperty('--cursor-x', `${pending.x}%`)
      node.style.setProperty('--cursor-y', `${pending.y}%`)
      pending = null
    }

    const onMove = (event) => {
      const rect = node.getBoundingClientRect()
      pending = {
        x: (((event.clientX - rect.left) / rect.width) * 100).toFixed(2),
        y: (((event.clientY - rect.top) / rect.height) * 100).toFixed(2),
      }
      if (!frame) frame = requestAnimationFrame(flush)
    }

    window.addEventListener('pointermove', onMove, { passive: true })
    return () => {
      window.removeEventListener('pointermove', onMove)
      if (frame) cancelAnimationFrame(frame)
    }
  }, [targetRef])
}

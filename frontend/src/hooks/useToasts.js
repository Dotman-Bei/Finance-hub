import { useCallback, useEffect, useRef, useState } from 'react'

const MAX_VISIBLE = 4

/** Transient notification queue with auto-dismiss and cleanup on unmount. */
export default function useToasts({ ttl = 7000 } = {}) {
  const [toasts, setToasts] = useState([])
  const timersRef = useRef(new Map())

  const dismiss = useCallback((id) => {
    setToasts((current) => current.filter((toast) => toast.id !== id))
    const timer = timersRef.current.get(id)
    if (timer) {
      clearTimeout(timer)
      timersRef.current.delete(id)
    }
  }, [])

  const push = useCallback(
    (toast) => {
      const id = crypto.randomUUID()
      setToasts((current) => [{ ...toast, id }, ...current].slice(0, MAX_VISIBLE))
      timersRef.current.set(id, setTimeout(() => dismiss(id), toast.ttl ?? ttl))
      return id
    },
    [dismiss, ttl]
  )

  useEffect(() => {
    const timers = timersRef.current
    return () => {
      timers.forEach(clearTimeout)
      timers.clear()
    }
  }, [])

  return { toasts, push, dismiss }
}

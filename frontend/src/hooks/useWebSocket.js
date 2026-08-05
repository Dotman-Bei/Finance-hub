import { useCallback, useEffect, useRef, useState } from 'react'

const READY = { CONNECTING: 'connecting', OPEN: 'open', CLOSED: 'closed', FAILED: 'failed' }

function resolveUrl(path) {
  const explicit = import.meta.env.VITE_WS_URL
  if (explicit) return explicit
  const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${protocol}://${window.location.host}${path}`
}

/**
 * Live exception feed — `WS /ws/exceptions` (§12).
 *
 * Reconnects with capped exponential backoff. When the socket cannot be
 * established the hook reports `failed` and goes quiet: it never synthesises
 * events to make the feed look alive. A silent feed is visibly silent; a
 * simulated one is indistinguishable from real work arriving.
 *
 * The gateway sends a `connection.established` frame stating whether its own
 * upstream event bus is reachable, so "socket open" and "events actually
 * flowing" stay distinguishable.
 */
export default function useWebSocket(path = '/ws/exceptions', { onMessage, enabled = true } = {}) {
  const [status, setStatus] = useState(READY.CONNECTING)
  const [busLive, setBusLive] = useState(false)
  const [lastMessage, setLastMessage] = useState(null)
  const [attempts, setAttempts] = useState(0)

  const socketRef = useRef(null)
  const timerRef = useRef(null)
  const attemptsRef = useRef(0)
  const closedByUsRef = useRef(false)

  // Keep the latest callback without forcing a reconnect on every render.
  const handlerRef = useRef(onMessage)
  useEffect(() => {
    handlerRef.current = onMessage
  }, [onMessage])

  const connect = useCallback(() => {
    const maxAttempts = 8
    let socket

    try {
      socket = new WebSocket(resolveUrl(path))
    } catch {
      setStatus(READY.FAILED)
      return
    }

    socketRef.current = socket
    setStatus(READY.CONNECTING)

    socket.onopen = () => {
      attemptsRef.current = 0
      setAttempts(0)
      setStatus(READY.OPEN)
    }

    socket.onmessage = (event) => {
      let payload
      try {
        payload = JSON.parse(event.data)
      } catch {
        return
      }

      // The gateway's opening frame reports whether events can actually flow.
      if (payload.type === 'connection.established' || payload.type === 'heartbeat') {
        setBusLive(Boolean(payload.payload?.live))
        if (payload.type === 'heartbeat') return
      }

      setLastMessage(payload)
      handlerRef.current?.(payload)
    }

    socket.onerror = () => socket.close()

    socket.onclose = () => {
      socketRef.current = null
      setBusLive(false)
      if (closedByUsRef.current) return

      attemptsRef.current += 1
      setAttempts(attemptsRef.current)

      if (attemptsRef.current > maxAttempts) {
        // Give up loudly rather than reconnecting forever in the background.
        setStatus(READY.FAILED)
        return
      }

      setStatus(READY.CLOSED)
      const backoff = Math.min(30000, 1000 * 2 ** (attemptsRef.current - 1))
      timerRef.current = setTimeout(connect, backoff)
    }
  }, [path])

  useEffect(() => {
    if (!enabled) return undefined

    closedByUsRef.current = false
    connect()

    return () => {
      closedByUsRef.current = true
      clearTimeout(timerRef.current)
      socketRef.current?.close()
      socketRef.current = null
    }
  }, [connect, enabled])

  const reconnect = useCallback(() => {
    attemptsRef.current = 0
    setAttempts(0)
    clearTimeout(timerRef.current)
    socketRef.current?.close()
    connect()
  }, [connect])

  const send = useCallback((data) => {
    const socket = socketRef.current
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(typeof data === 'string' ? data : JSON.stringify(data))
      return true
    }
    return false
  }, [])

  return {
    status,
    lastMessage,
    send,
    reconnect,
    attempts,
    /** Socket open *and* the gateway's upstream bus reachable. */
    isLive: status === READY.OPEN && busLive,
    isConnected: status === READY.OPEN,
  }
}

export { READY as WS_STATUS }

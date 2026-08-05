import axios from 'axios'

/**
 * Shared HTTP client for the reporting_api gateway (Subsystem 4 backend).
 *
 * Base URL is empty by default so requests go same-origin and hit the Vite dev
 * proxy (see vite.config.js) or the reverse proxy in front of the container.
 * Set VITE_API_BASE_URL to point at a gateway on another host.
 */
const axiosClient = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL ?? '',
  timeout: Number(import.meta.env.VITE_API_TIMEOUT ?? 12000),
  headers: { 'Content-Type': 'application/json' },
})

const TOKEN_KEY = 'financehub.jwt'
const ROLE_KEY = 'financehub.role'

export const auth = {
  getToken: () => localStorage.getItem(TOKEN_KEY) || '',
  setToken: (token) =>
    token ? localStorage.setItem(TOKEN_KEY, token) : localStorage.removeItem(TOKEN_KEY),
  getRole: () => localStorage.getItem(ROLE_KEY) || 'FINANCE_MANAGER',
  setRole: (role) => localStorage.setItem(ROLE_KEY, role),
}

// RBAC (§3.4.1): the gateway is the enforcement point. We attach the JWT and
// echo the active role so the API can scope the response to the caller's view.
axiosClient.interceptors.request.use((config) => {
  const token = auth.getToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  config.headers['X-FinanceHub-Role'] = auth.getRole()
  return config
})

/** Error shape every caller can rely on. */
export class ApiError extends Error {
  constructor(message, { status = 0, offline = false, detail = null } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.offline = offline
    this.detail = detail
  }
}

/**
 * Statuses a proxy returns when it cannot reach the upstream service.
 *
 * A browser talking through the Vite dev proxy or nginx never sees a network
 * error when the gateway is down — the proxy answers on its behalf. Treating
 * these as "unreachable" is what lets the UI say "start reporting_api" instead
 * of the useless "Internal Server Error".
 */
const UPSTREAM_DOWN = new Set([500, 502, 503, 504])

axiosClient.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error.response?.status ?? 0
    const detail = error.response?.data?.detail ?? null

    // No response at all, or a proxy reporting it could not reach the gateway.
    const offline = !error.response || (UPSTREAM_DOWN.has(status) && !detail)

    const message = offline
      ? 'Reporting API unreachable'
      : detail || error.response?.statusText || 'Request failed'

    return Promise.reject(new ApiError(message, { status, offline, detail }))
  }
)

export default axiosClient

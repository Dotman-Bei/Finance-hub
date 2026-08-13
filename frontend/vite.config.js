import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// The reporting_api gateway (Subsystem 4 backend) listens on :8000 per docker-compose.
// In dev we proxy its routes so the SPA can call them same-origin (no CORS dance).
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const target = env.VITE_PROXY_TARGET || 'http://localhost:8000'
  const proxied = ['/metrics', '/exceptions', '/reports', '/auth', '/reconcile']

  return {
    plugins: [react()],
    server: {
      host: '0.0.0.0',
      port: 3000,
      proxy: {
        ...Object.fromEntries(
          proxied.map((path) => [path, { target, changeOrigin: true }])
        ),
        '/ws': { target: target.replace(/^http/, 'ws'), ws: true },
      },
    },
    build: {
      outDir: 'dist',
      sourcemap: mode !== 'production',
      rollupOptions: {
        output: {
          // The two charting libraries dominate the bundle; split them out so
          // the app shell is not re-downloaded when only app code changes.
          // Recharts is matched before React because it pulls in d3 via
          // victory-vendor, and react-chartjs-2 before the React runtime.
          manualChunks(id) {
            if (!id.includes('node_modules')) return undefined
            if (/[\\/](recharts|victory-vendor|d3-)/.test(id)) return 'recharts'
            if (/[\\/](chart\.js|react-chartjs-2)[\\/]/.test(id)) return 'chartjs'
            if (/[\\/](react|react-dom|scheduler)[\\/]/.test(id)) return 'react'
            return 'vendor'
          },
        },
      },
    },
  }
})

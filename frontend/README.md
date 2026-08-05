# FinanceHub — Web-Based Reporting Dashboard

Subsystem 4 of FinanceHub (Objective 4): on-demand reconciliation visibility —
KPIs, match-rate analytics, exception management and audit-ready PDF reports.

React SPA, built to the stack in `build.md` §2 and styled to the **Horizon**
visual language in `frontend.md`.

---

## Quickstart

```bash
npm install
cp .env.example .env       # optional — the defaults work as-is
npm run dev                # http://localhost:3000
```

The dev server proxies `/metrics`, `/exceptions`, `/reports`, `/auth` and `/ws`
to `http://localhost:8000` (the `reporting_api` gateway). **If the gateway is
not running the dashboard still works** — every panel falls back to a
deterministic synthetic corpus and a banner says so. That makes Phase 4
demoable before Phases 1–3 are deployed.

```bash
npm run build              # production bundle → dist/
npm run preview            # serve the bundle locally
```

### Docker

```bash
docker build -t financehub-frontend .
docker run -p 3000:3000 financehub-frontend
```

The image serves the static bundle from nginx and reverse-proxies the gateway
(`nginx.conf`), so the SPA calls it same-origin — matching the `frontend`
service block in the root `docker-compose.yml`.

---

## API contract consumed

| Method | Endpoint | Used by |
|--------|----------|---------|
| `GET` | `/metrics/kpi` | `KpiSummaryCards`, `Hero` |
| `GET` | `/metrics/match-rate?from=&to=` | `MatchRateChart` |
| `GET` | `/exceptions` | `ExceptionPanel` |
| `POST` | `/exceptions/{id}/resolve` | `ExceptionPanel` accept / reject / edit |
| `GET` | `/reports` | `ReportsPanel` history |
| `POST` | `/reports/generate` | `ReportsPanel` generator |
| `GET` | `/reports/{id}/download` | PDF download |
| `WS` | `/ws/exceptions` | Live exception toasts |

`GET /exceptions` accepts either a bare array or a `{ items: [] }` envelope, and
the transaction may be nested (`transaction`) or flattened onto the queue row —
`normalizeException` in [src/api/reportingApi.js](src/api/reportingApi.js)
reconciles both into one shape.

Every request carries `Authorization: Bearer <jwt>` (when a token is stored) and
`X-FinanceHub-Role`. The gateway remains the RBAC enforcement point; the client
only scopes the view.

---

## Roles (§3.4.1)

Switchable from the nav pill. Each scopes the affordances the UI offers:

| Role | Resolve exceptions | Generate reports | Run reconciliation |
|------|--------------------|------------------|--------------------|
| Finance Manager | ✅ | ✅ | ✅ |
| Auditor | — | ✅ | — |
| System Administrator | ✅ | ✅ | ✅ |

---

## Layout

```
src/
├── api/
│   ├── axiosClient.js      # instance, JWT + role interceptors, ApiError
│   ├── reportingApi.js     # endpoint bindings + offline fallback + normalisation
│   └── demoData.js         # deterministic synthetic corpus
├── components/
│   ├── KpiSummaryCards.jsx # live KPI tiles
│   ├── MatchRateChart.jsx  # Recharts trend/volume + Chart.js mix, tabbed glass
│   ├── ExceptionPanel.jsx  # filterable queue, inline accept/reject/edit
│   ├── ReportsPanel.jsx    # PDF generator + history
│   ├── FloatingNav.jsx     # pill nav + role switcher + CTA
│   ├── Hero.jsx  Footer.jsx  ToastStack.jsx  RoleSwitcher.jsx
│   └── ui/                 # Icon, Logo, Section
├── hooks/
│   ├── useWebSocket.js     # live feed, backoff, simulated fallback
│   ├── useCursorGradient.js  useScrollSpy.js  useToasts.js
└── lib/
    ├── constants.js        # mirrors shared/models/enums.py
    └── format.js           # currency / percent / relative time
```

---

## Design system

Tokens live in [tailwind.config.js](tailwind.config.js); components only ever
reference token names, never raw hex.

- **Accent** `#FF8A65` · **Energy gradient** electric blue → vivid magenta
- **Type** Plus Jakarta Sans — bold, tight tracking for headings; generous
  leading for body
- **Glass** `bg-surface/70` + `backdrop-blur-glass` (20px) + hairline
  `border-outline-variant`, depth from soft shadow rather than stark containers
- **Rhythm** 40px (`space-rhythm`) between blocks, 80px between sections
- **Logos and copyright** always solid black for contrast
- **Cursor** native throughout — no custom cursor. Buttons and `select`s opt
  into `pointer`, disabled controls into `not-allowed`
- **Cursor-reactive gradients** `--cursor-x/y` drive the `.cursor-aura` washes
  in the hero and footer

Motion is suppressed under `prefers-reduced-motion`, and the pointer-tracking
gradients are skipped on coarse pointers.

/** Shared vocabulary — mirrors shared/models/enums.py and db/schema.sql. */

export const EXCEPTION_CATEGORIES = [
  'PARTIAL_PAYMENT',
  'SPLIT_SETTLEMENT',
  'MISSING_REFERENCE_CODE',
  'TIMING_DIFFERENCE',
]

/** Category → label, resolution pathway (§10 table) and chart colour. */
export const CATEGORY_META = {
  PARTIAL_PAYMENT: {
    label: 'Partial Payment',
    short: 'Partial',
    pathway: 'Propose partial-match journal entry; flag remaining balance for follow-up',
    color: '#FF8A65',
    chip: 'border-primary-200 bg-primary-50 text-primary-700',
  },
  SPLIT_SETTLEMENT: {
    label: 'Split Settlement',
    short: 'Split',
    pathway: 'Open multi-line allocation resolution across the target obligations',
    color: '#7B5BF5',
    chip: 'border-[#DDD6FE] bg-[#F5F3FF] text-[#5B3FD1]',
  },
  MISSING_REFERENCE_CODE: {
    label: 'Missing Reference Code',
    short: 'No Ref',
    pathway: 'Surface likely counterpart candidates for manual reference assignment',
    color: '#0A84FF',
    chip: 'border-[#BFDBFE] bg-[#EFF6FF] text-[#0A5FCC]',
  },
  TIMING_DIFFERENCE: {
    label: 'Timing Difference',
    short: 'Timing',
    pathway: 'Suggest matching across accounting periods; hold pending settlement date',
    color: '#E935C1',
    chip: 'border-[#F9CFEE] bg-[#FDF2FA] text-[#B3218F]',
  },
}

export const EXCEPTION_STATES = {
  OPEN: { label: 'Open', chip: 'border-outline-variant bg-surface-dim text-on-surface-variant' },
  SUGGESTED: { label: 'Suggested', chip: 'border-[#FDE6C7] bg-[#FFF9F0] text-[#A9651A]' },
  RESOLVED: { label: 'Resolved', chip: 'border-[#BFE9E2] bg-[#F0FBF9] text-[#0B7A6E]' },
  REJECTED: { label: 'Rejected', chip: 'border-[#F7CDCE] bg-[#FEF4F4] text-[#B03539]' },
}

/** RBAC (§3.4.1) — three roles, each with a scoped view of the dashboard. */
export const ROLES = {
  FINANCE_MANAGER: {
    label: 'Finance Manager',
    short: 'Manager',
    blurb: 'Full reconciliation control and exception resolution.',
    can: { resolveExceptions: true, generateReports: true, runReconciliation: true, viewAudit: false },
  },
  AUDITOR: {
    label: 'Auditor',
    short: 'Auditor',
    blurb: 'Read-only visibility with full audit trail and report access.',
    can: { resolveExceptions: false, generateReports: true, runReconciliation: false, viewAudit: true },
  },
  SYSTEM_ADMINISTRATOR: {
    label: 'System Administrator',
    short: 'Admin',
    blurb: 'Pipeline health, thresholds and every downstream view.',
    can: { resolveExceptions: true, generateReports: true, runReconciliation: true, viewAudit: true },
  },
}

export const REPORT_TYPES = [
  { value: 'RECONCILIATION_SUMMARY', label: 'Reconciliation Summary' },
  { value: 'EXCEPTION_LOG', label: 'Exception Log & Resolutions' },
  { value: 'MATCH_RATE_ANALYTICS', label: 'Match-Rate Analytics' },
  { value: 'AUDIT_TRAIL', label: 'Full Audit Trail' },
]

export const RANGE_PRESETS = [
  { value: '7d', label: '7D', days: 7 },
  { value: '30d', label: '30D', days: 30 },
  { value: '90d', label: '90D', days: 90 },
]

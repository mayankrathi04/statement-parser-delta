export type Card = {
  id: number
  issuer: string
  product: string | null
  masked_number: string
  last4: string
  display_name: string
  txn_count: number
  first_txn: string | null
  last_txn: string | null
}

export type Txn = {
  id: number
  txn_date: string
  txn_time: string | null
  description: string
  merchant: string
  category: string
  amount: number
  direction: 'debit' | 'credit'
  signed: number
  is_emi: boolean
  reward_points: number | null
  fcy_currency: string | null
  fcy_amount: number | null
  card: string
  card_id: number
  statement_date: string | null
  statement_period_start: string | null
  statement_period_end: string | null
  statement_month: string | null
}

export type CardStatement = {
  id: number
  month: string
  source_file: string
  statement_date: string | null
  period_start: string | null
  period_end: string | null
  txns: number
  spend: number
  total_dues: number
  confidence: number
  imported_at: string | null
}

export type CardRow = Card & {
  history: CardStatement[]
  statements: number
  last_statement: string | null
  password_set: boolean
  password_source: 'manual' | 'learned' | null
  password_updated_at: string | null
  sender_ids: string[]
  subject_patterns: string[]
}

export type Slice = { label: string; value: number; n: number; card_id?: number; account_id?: number }

export type RewardCard = { card_id: number; label: string; points: number; n: number }
export type EmiRow = {
  txn_date: string; description: string; merchant: string
  amount: number; card: string; card_id: number
}
export type FcyRow = EmiRow & { currency: string; fcy_amount: number }

export type Analytics = {
  rewards: {
    total_points: number
    earning_txns: number
    by_card: RewardCard[]
    by_month: { month: string; points: number }[]
  }
  emi: { count: number; total: number; rows: EmiRow[] }
  fcy: { count: number; total_inr: number; rows: FcyRow[] }
  totals: {
    spend: number; payments: number; net: number; txn_count: number
    avg_txn: number; largest: number; months: number; avg_month: number
  }
  monthly: { month: string; spend: number; payments: number }[]
  by_category: Slice[]
  by_card: Slice[]
  top_merchants: Slice[]
}

export type Check = { name: string; passed: boolean; detail: string; severity: string }

export type Step = {
  seq: number
  name: string
  status: 'ok' | 'failed' | 'skipped' | 'running'
  detail: string
  ms: number | null
}

export type IngestFile = {
  id: number
  filename: string
  status: 'ok' | 'failed' | 'skipped' | 'running'
  issuer: string | null
  product: string | null
  card: string | null
  template_id: string | null
  encrypted: boolean
  txn_count: number | null
  confidence: number | null
  error: string | null
  checks: Check[]
  steps: Step[]
}

export type Pending = {
  id: number
  filename: string
  card: string | null
  issuer: string | null
  template_id: string | null
  encrypted: boolean
  txn_count: number | null
  confidence: number | null
  statement_date: string | null
  period_start: string | null
  period_end: string | null
  is_duplicate: boolean
  error: string | null
  checks: Check[]
  steps: Step[]
}

export type Run = {
  id: number
  kind: string
  status: string
  started_at: string
  finished_at: string | null
  note: string | null
  files: number
  ok: number
  pending: number
}

export type Bootstrap = {
  cards: Card[]
  bounds: { min: string | null; max: string | null }
  statements: { id: number; card: string; statement_date: string; confidence: number }[]
  mailboxes_configured: boolean
}

export type Filters = { cards: number[]; allCards: number; from: string | null; to: string | null }

export type BankStatementRow = {
  id: number
  source_file: string
  parser_id: string
  period_start: string
  period_end: string
  coverage_start: string | null
  coverage_end: string | null
  opening_balance: number
  closing_balance: number
  withdrawals: number
  deposits: number
  confidence: number
  imported_at: string
  txns: number
  checks: Check[]
}

export type BankAccount = {
  id: number
  bank_code: string
  bank_name: string
  masked_number: string
  last4: string
  account_holder: string | null
  account_type: string | null
  product: string | null
  branch: string | null
  display_name: string
  statements: number
  txn_count: number
  first_txn: string | null
  last_txn: string | null
  history: BankStatementRow[]
}

export type BankTxn = {
  id: number
  txn_date: string
  value_date: string | null
  description: string
  reference: string | null
  counterparty: string
  category: string
  derived_category: string
  category_override: string | null
  category_is_override: boolean
  amount: number
  direction: 'debit' | 'credit'
  signed: number
  balance: number
  page: number
  account_id: number
  account: string
  statement_period_start: string
  statement_period_end: string
}

export type BankAnalytics = {
  totals: {
    withdrawals: number
    deposits: number
    net: number
    txn_count: number
    avg_debit: number
    largest_debit: number
    opening_balance: number
    closing_balance: number
  }
  monthly: { month: string; withdrawals: number; deposits: number }[]
  by_category: Slice[]
  deposits_by_category: Slice[]
  by_account: (Slice & { account_id: number })[]
  top_counterparties: Slice[]
}

export type BankBootstrap = {
  accounts: BankAccount[]
  bounds: { min: string | null; max: string | null }
}

export type BankFilters = {
  accounts: number[]
  allAccounts: number
  from: string | null
  to: string | null
}

function qs(f: Filters): string {
  const p = new URLSearchParams()
  if (f.cards.length && f.cards.length !== f.allCards) p.set('cards', f.cards.join(','))
  if (f.from) p.set('from', f.from)
  if (f.to) p.set('to', f.to)
  const s = p.toString()
  return s ? `?${s}` : ''
}

function bankQs(f: BankFilters): string {
  const p = new URLSearchParams()
  if (f.accounts.length && f.accounts.length !== f.allAccounts) p.set('accounts', f.accounts.join(','))
  if (f.from) p.set('from', f.from)
  if (f.to) p.set('to', f.to)
  const s = p.toString()
  return s ? `?${s}` : ''
}

export type Profile = {
  full_name: string
  dob: string
  updated_at: string | null
  derives: number
}

export type Mailbox = {
  id: number
  address: string
  provider: string
  status: 'connected' | 'failed' | 'unknown'
  last_error: string | null
  last_checked: string | null
  last_sync: string | null
  added_at: string | null
  secret_ok: boolean
}

/** A failed `fetch` rejects with a bare TypeError reading "Failed to fetch",
 *  which tells the user nothing. It means the backend is not listening — the
 *  usual state in development, because `npm run dev` starts only Vite — so it is
 *  translated into the thing they actually need to do about it. */
const UNREACHABLE =
  'Cannot reach the API — is the backend running? Start it with ' +
  '`python -m sparser serve --db statements.db`, or use ./scripts/dev.sh to run both.'

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  let r: Response
  try {
    r = await fetch(url, init)
  } catch {
    throw new Error(UNREACHABLE)
  }
  const data = await r.json().catch(() => ({}))
  if (!r.ok) {
    const detail = (data as { detail?: string }).detail
    throw new Error(detail ?? `${r.status} ${r.statusText}`)
  }
  return data as T
}

const get = <T,>(url: string) => request<T>(url)

const post = <T,>(url: string, body: unknown) =>
  request<T>(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

export const api = {
  bootstrap: () => get<Bootstrap>('/api/bootstrap'),
  analytics: (f: Filters) => get<Analytics>(`/api/analytics${qs(f)}`),
  transactions: (f: Filters) => get<Txn[]>(`/api/transactions${qs(f)}`),
  exportUrl: (f: Filters) => `/api/export${qs(f)}`,
  bankBootstrap: () => get<BankBootstrap>('/api/bank/bootstrap'),
  bankAnalytics: (f: BankFilters) => get<BankAnalytics>(`/api/bank/analytics${bankQs(f)}`),
  bankTransactions: (f: BankFilters) => get<BankTxn[]>(`/api/bank/transactions${bankQs(f)}`),
  updateBankTransactionCategory: (id: number, category: string | null) =>
    request<Pick<BankTxn, 'id' | 'category' | 'derived_category' | 'category_override' | 'category_is_override'>>(
      `/api/bank/transactions/${id}/category`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ category }),
      },
    ),
  bankExportUrl: (f: BankFilters) => `/api/bank/export${bankQs(f)}`,
  bankRuns: () => get<{ runs: Run[]; busy: boolean }>('/api/bank/runs'),
  bankRun: (id: number) => get<{ run: Run; files: IngestFile[] }>(`/api/bank/runs/${id}`),
  bankPending: () => get<{ pending: Pending[] }>('/api/bank/pending'),
  discardBankPending: (file_ids: number[]) =>
    post<{ discarded: number }>('/api/bank/pending/discard', { file_ids }),
  reevaluateBankPending: (file_ids: number[] = [], password = '') =>
    post<{ status: string; count: number | null }>('/api/bank/pending/reevaluate', {
      file_ids, password,
    }),
  approveBankStatements: (file_ids: number[], password = '') =>
    post<{ status: string; count: number }>('/api/bank/ingest/approve', { file_ids, password }),
  uploadBankStatements: (files: File[], password = '') => {
    const body = new FormData()
    files.forEach((file) => body.append('files', file))
    if (password) body.append('password', password)
    return request<{ status: string; files: string[] }>('/api/bank/ingest/upload', {
      method: 'POST',
      body,
    })
  },
  runs: () => get<{ runs: Run[]; busy: boolean }>('/api/runs'),
  run: (id: number) => get<{ run: Run; files: IngestFile[] }>(`/api/runs/${id}`),
  fetchMail: (body: Record<string, unknown>) =>
    post<{ status: string }>('/api/ingest/fetch', body),
  scanMail: (body: Record<string, unknown>) =>
    post<{ status: string }>('/api/ingest/scan', body),
  scanLocal: (body: Record<string, unknown>) =>
    post<{ status: string }>('/api/ingest/scan-local', body),
  pending: () => get<{ pending: Pending[] }>('/api/pending'),
  pendingPdfUrl: (id: number) => `/api/pending/${id}/pdf`,
  discard: (file_ids: number[]) =>
    post<{ discarded: number }>('/api/pending/discard', { file_ids }),
  reevaluatePending: (file_ids: number[] = []) =>
    post<{ status: string; count: number | null }>('/api/pending/reevaluate', { file_ids }),
  approve: (file_ids: number[]) =>
    post<{ status: string; count: number }>('/api/ingest/approve', { file_ids }),
  importLocal: (body: Record<string, unknown>) =>
    post<{ status: string; files: string[] }>('/api/ingest/local', body),
  mailboxes: () =>
    get<{ mailboxes: Mailbox[]; env_configured: boolean; key_file: string }>('/api/mailboxes'),
  addMailbox: (address: string, app_password: string) =>
    post<{ status: string; detail: string }>('/api/mailboxes', { address, app_password }),
  testMailbox: (id: number) =>
    post<{ status: string; detail: string }>(`/api/mailboxes/${id}/test`, {}),
  removeMailbox: (id: number) =>
    request<{ status: string }>(`/api/mailboxes/${id}`, { method: 'DELETE' }),
  cards: () => get<{ cards: CardRow[] }>('/api/cards'),
  profile: () => get<Profile>('/api/profile'),
  saveProfile: (full_name: string, dob: string) =>
    request<Profile & { status: string }>('/api/profile', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ full_name, dob }),
    }),
  revealCardPassword: (id: number) => get<{ password: string }>(`/api/cards/${id}/password`),
  setCardPassword: (id: number, password: string) =>
    request<{ status: string }>(`/api/cards/${id}/password`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    }),
  clearCardPassword: (id: number) =>
    request<{ status: string }>(`/api/cards/${id}/password`, { method: 'DELETE' }),
  setCardMailRules: (id: number, sender_ids: string[], subject_patterns: string[]) =>
    request<{ status: string }>(`/api/cards/${id}/mail-rules`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sender_ids, subject_patterns }),
    }),
}

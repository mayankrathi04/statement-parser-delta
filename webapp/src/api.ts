export type Card = {
  id: number
  issuer: string
  product: string | null
  masked_number: string
  last4: string
  display_name: string
  member_id: number
  member_name: string
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
  derived_category: string
  category_override: string | null
  category_is_override: boolean
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
  by_member: { member_id: number; member: string; debits: number; credits: number; n: number }[]
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
  /** Whether the PDF is still on disk, so the run history can offer to open it. */
  pdf_available?: boolean
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
  /** What approving would add, and what the ledger already holds. Bank rows only. */
  new_txn_count?: number | null
  known_txn_count?: number | null
  confidence: number | null
  statement_date: string | null
  period_start: string | null
  period_end: string | null
  is_duplicate: boolean
  error: string | null
  /** Member this file was uploaded/fetched for, fixed at scan time. */
  member_id: number | null
  member_name: string | null
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

/** A category present on the rows in scope, with how many carry it. */
export type CategoryFacet = { name: string; n: number }

export type Bootstrap = {
  cards: Card[]
  bounds: { min: string | null; max: string | null }
  categories: CategoryFacet[]
  statements: { id: number; card: string; statement_date: string; confidence: number }[]
  mailboxes_configured: boolean
}

export type Filters = {
  cards: number[]; allCards: number; members: number[]; from: string | null; to: string | null
  /** Ticked category names. Everything ticked and nothing ticked are different. */
  categories: string[]
  allCategories: number
}

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
  member_id: number
  member_name: string
  statements: number
  txn_count: number
  first_txn: string | null
  last_txn: string | null
  history: BankStatementRow[]
  /** Mailbox scan rules, exactly as a card carries them. */
  sender_ids: string[]
  subject_patterns: string[]
  /** Whether a statement password is stored — never the value. */
  password_set: boolean
  password_source: 'manual' | 'learned' | null
  password_updated_at: string | null
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
  by_member: {
    member_id: number; member: string; withdrawals: number; deposits: number; n: number
  }[]
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
  net_by_category: Slice[]
  by_account: (Slice & { account_id: number })[]
  top_counterparties: Slice[]
}

export type BankBootstrap = {
  accounts: BankAccount[]
  bounds: { min: string | null; max: string | null }
  categories: CategoryFacet[]
}

export type BankFilters = {
  accounts: number[]
  allAccounts: number
  members: number[]
  from: string | null
  to: string | null
  /** Ticked category names. Everything ticked and nothing ticked are different. */
  categories: string[]
  allCategories: number
}

/** The top-bar member picker, as a query string. Every listing endpoint takes it,
 *  so one selection scopes analytics, cards, accounts and the pipelines alike. */
function memberQs(members: Iterable<number>): string {
  const ids = [...members]
  return ids.length ? `?members=${ids.join(',')}` : ''
}

/** Add the category filter to a query string, if the user narrowed one.
 *
 *  Repeated `categories=` values, never one comma-joined string, because a
 *  category name may itself contain a comma. Everything ticked is the same as
 *  no filter and sends nothing, which keeps the common URL short. Nothing
 *  ticked is the opposite and must still reach the server: a query string
 *  cannot carry an empty repeated parameter, so one blank value says it.
 */
function appendCategories(p: URLSearchParams, picked: string[], total: number): void {
  if (picked.length === total) return
  if (!picked.length) p.append('categories', '')
  else picked.forEach((name) => p.append('categories', name))
}

function qsParams(f: Filters): URLSearchParams {
  const p = new URLSearchParams()
  if (f.cards.length && f.cards.length !== f.allCards) p.set('cards', f.cards.join(','))
  if (f.members.length) p.set('members', f.members.join(','))
  if (f.from) p.set('from', f.from)
  if (f.to) p.set('to', f.to)
  appendCategories(p, f.categories, f.allCategories)
  return p
}

function bankQsParams(f: BankFilters): URLSearchParams {
  const p = new URLSearchParams()
  if (f.accounts.length && f.accounts.length !== f.allAccounts) p.set('accounts', f.accounts.join(','))
  if (f.members.length) p.set('members', f.members.join(','))
  if (f.from) p.set('from', f.from)
  if (f.to) p.set('to', f.to)
  appendCategories(p, f.categories, f.allCategories)
  return p
}

const qs = (f: Filters): string => query(qsParams(f))
const bankQs = (f: BankFilters): string => query(bankQsParams(f))

const query = (p: URLSearchParams): string => {
  const s = p.toString()
  return s ? `?${s}` : ''
}

/** A filter set collapsed to what it actually asks the server for.
 *
 *  Two filter sets share a key when they select the same rows, whatever order
 *  the ids or category names happen to arrive in — the facet list is re-read
 *  after every category edit and comes back ordered by count, so an edit
 *  re-orders it without changing the query at all. Pagers reset on this key
 *  rather than on the filter object, which is why retagging a transaction no
 *  longer throws the reader back to page one.
 */
function key(p: URLSearchParams): string {
  return [...p.entries()]
    .map(([name, value]) => `${name}=${value.split(',').sort().join(',')}`)
    .sort()
    .join('&')
}

export const filtersKey = (f: Filters): string => key(qsParams(f))
export const bankFiltersKey = (f: BankFilters): string => key(bankQsParams(f))

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
  member_id: number
  member_name: string
  /** Which pipelines sweep this mailbox. Both false pauses it without deleting it. */
  use_for_cards: boolean
  use_for_bank: boolean
}

/** The fallback mailbox search — what a scan uses for anything with no rules of its own. */
export type ScanDefaults = {
  senders: string[]
  subjects: string[]
  /** Whether these were edited away from what the project ships with. */
  customised: boolean
  built_in_senders: string[]
  built_in_subjects: string[]
}

/** One shared sub-category list drives both the card and bank ledgers. */
export type Category = {
  id: number
  name: string
  pattern: string | null
  applies_to: 'both' | 'cards' | 'bank'
  position: number
  created_at: string
  usage: { cards: number; bank: number }
  /** The major this rolls up into; null means it is still unfiled. */
  major_id: number | null
  major: string | null
  /** Rows carried over by a rename — only present on an update response. */
  moved?: number
}

/**
 * A heading in the household's own list. Carries no pattern: a major never
 * matches a narration itself, it collects what its subs catch.
 */
export type MajorCategory = {
  id: number
  name: string
  position: number
  created_at: string
  children: Category[]
}

/** A label the card issuer printed itself — shown read-only; we cannot edit it. */
export type ProviderCategory = { name: string; rows: number; sources: string[] }

export type Member = { id: number; name: string; is_default: number; created_at: string }
/** A member plus what is filed under them — only the Members tab needs the counts. */
export type MemberDetail = Member & { cards: number; bank_accounts: number; mailboxes: number }
export type PortalUser = {
  id: number; username: string; display_name: string; created_at: string; members: Member[]
}

/** A failed `fetch` rejects with a bare TypeError reading "Failed to fetch",
 *  which tells the user nothing. It means the backend is not listening — the
 *  usual state in development, because `npm run dev` starts only Vite — so it is
 *  translated into the thing they actually need to do about it. */
const UNREACHABLE =
  'Cannot reach the API — is the backend running? Start it with ' +
  '`python -m sparser serve --db data/statements.db`, or use ./scripts/dev.sh to run both.'

/** An API failure carrying its HTTP status.
 *
 *  ``status === 0`` means the request never reached the server. Callers must be
 *  able to tell that apart from a rejected token: only the latter should end a
 *  session, or restarting the backend would sign the user out.
 */
export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }

  get unreachable(): boolean {
    return this.status === 0
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  let r: Response
  try {
    const headers = new Headers(init?.headers)
    const token = localStorage.getItem('sparser_token')
    if (token) headers.set('Authorization', `Bearer ${token}`)
    r = await fetch(url, { ...init, headers })
  } catch {
    throw new ApiError(UNREACHABLE, 0)
  }
  const data = await r.json().catch(() => ({}))
  if (!r.ok) {
    if (r.status === 401 && !url.startsWith('/api/auth/')) {
      window.dispatchEvent(new Event('sparser-auth-required'))
    }
    const detail = (data as { detail?: string }).detail
    throw new ApiError(detail ?? `${r.status} ${r.statusText}`, r.status)
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
  authStatus: () => get<{ registration_required: boolean }>('/api/auth/status'),
  me: () => get<PortalUser>('/api/auth/me'),
  logout: () => post<{ status: string }>('/api/auth/logout', {}),
  login: (username: string, password: string) =>
    post<{ token: string; user: PortalUser }>('/api/auth/login', { username, password }),
  register: (username: string, password: string, display_name: string) =>
    post<{ token: string; user: PortalUser }>('/api/auth/register', { username, password, display_name }),
  members: () => get<{ members: MemberDetail[] }>('/api/members'),
  addMember: (name: string) => post<Member>('/api/members', { name }),
  renameMember: (id: number, name: string) =>
    request<Member>(`/api/members/${id}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }),
    }),
  setDefaultMember: (id: number) =>
    request<Member>(`/api/members/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_default: true }),
    }),
  removeMember: (id: number) =>
    request<{ status: string }>(`/api/members/${id}`, { method: 'DELETE' }),
  bootstrap: (members: Iterable<number> = []) =>
    get<Bootstrap>(`/api/bootstrap${memberQs(members)}`),
  analytics: (f: Filters) => get<Analytics>(`/api/analytics${qs(f)}`),
  transactions: (f: Filters) => get<Txn[]>(`/api/transactions${qs(f)}`),
  exportUrl: (f: Filters) => authenticatedUrl(`/api/export${qs(f)}`),
  bankBootstrap: (members: Iterable<number> = []) =>
    get<BankBootstrap>(`/api/bank/bootstrap${memberQs(members)}`),
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
  updateBankTransactionCategories: (ids: number[], category: string | null) =>
    request<{
      updated: number
      rows: Pick<BankTxn,
        'id' | 'category' | 'derived_category' | 'category_override' | 'category_is_override'>[]
    }>('/api/bank/transactions/category', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids, category }),
    }),
  bankExportUrl: (f: BankFilters) => authenticatedUrl(`/api/bank/export${bankQs(f)}`),
  bankRuns: () => get<{ runs: Run[]; busy: boolean }>('/api/bank/runs'),
  bankRun: (id: number) => get<{ run: Run; files: IngestFile[] }>(`/api/bank/runs/${id}`),
  bankPending: () => get<{ pending: Pending[] }>('/api/bank/pending'),
  discardBankPending: (file_ids: number[]) =>
    post<{ discarded: number }>('/api/bank/pending/discard', { file_ids }),
  reevaluateBankPending: (file_ids: number[] = [], password = '') =>
    post<{ status: string; count: number | null }>('/api/bank/pending/reevaluate', {
      file_ids, password,
    }),
  approveBankStatements: (file_ids: number[], password = '', member_id?: number) =>
    post<{ status: string; count: number }>('/api/bank/ingest/approve', {
      file_ids, password, member_id,
    }),
  uploadBankStatements: (files: File[], password = '', member_id?: number) => {
    const body = new FormData()
    files.forEach((file) => body.append('files', file))
    if (password) body.append('password', password)
    if (member_id) body.append('member_id', String(member_id))
    return request<{ status: string; files: string[] }>('/api/bank/ingest/upload', {
      method: 'POST',
      body,
    })
  },
  scanBankMail: (body: Record<string, unknown>) =>
    post<{ status: string }>('/api/bank/ingest/scan', body),
  scanBankLocal: (body: Record<string, unknown>) =>
    post<{ status: string; files: string[] }>('/api/bank/ingest/scan-local', body),
  setBankAccountMailRules: (id: number, sender_ids: string[], subject_patterns: string[]) =>
    request<{ status: string }>(`/api/bank/accounts/${id}/mail-rules`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sender_ids, subject_patterns }),
    }),
  revealBankAccountPassword: (id: number) =>
    get<{ password: string }>(`/api/bank/accounts/${id}/password`),
  setBankAccountPassword: (id: number, password: string) =>
    request<{ status: string }>(`/api/bank/accounts/${id}/password`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    }),
  clearBankAccountPassword: (id: number) =>
    request<{ status: string }>(`/api/bank/accounts/${id}/password`, { method: 'DELETE' }),
  scanDefaults: (kind: 'cards' | 'bank') => get<ScanDefaults>(`/api/scan-defaults/${kind}`),
  saveScanDefaults: (kind: 'cards' | 'bank', sender_ids: string[], subject_patterns: string[]) =>
    request<ScanDefaults>(`/api/scan-defaults/${kind}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sender_ids, subject_patterns }),
    }),
  resetScanDefaults: (kind: 'cards' | 'bank') =>
    request<ScanDefaults>(`/api/scan-defaults/${kind}`, { method: 'DELETE' }),
  setMailboxScope: (id: number, use_for_cards: boolean, use_for_bank: boolean) =>
    request<{ status: string; use_for_cards: boolean; use_for_bank: boolean }>(
      `/api/mailboxes/${id}/scope`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ use_for_cards, use_for_bank }),
      },
    ),
  runs: () => get<{ runs: Run[]; busy: boolean }>('/api/runs'),
  run: (id: number) => get<{ run: Run; files: IngestFile[] }>(`/api/runs/${id}`),
  fetchMail: (body: Record<string, unknown>) =>
    post<{ status: string }>('/api/ingest/fetch', body),
  scanMail: (body: Record<string, unknown>) =>
    post<{ status: string }>('/api/ingest/scan', body),
  scanLocal: (body: Record<string, unknown>) =>
    post<{ status: string }>('/api/ingest/scan-local', body),
  pending: () => get<{ pending: Pending[] }>('/api/pending'),
  // Opened by navigating a link, which cannot carry the Authorization header the
  // fetch wrapper adds, so the token rides the query string as the auth
  // middleware allows. Without it the tab shows "sign in to continue".
  pendingPdfUrl: (id: number) => authenticatedUrl(`/api/pending/${id}/pdf`),
  /** The PDF an already-imported statement was parsed from, decrypted on the way out. */
  statementPdfUrl: (id: number) => authenticatedUrl(`/api/statements/${id}/pdf`),
  /** Any file in a run's history — including one that failed before it became a statement. */
  ingestFilePdfUrl: (id: number) => authenticatedUrl(`/api/ingest/files/${id}/pdf`),
  bankStatementPdfUrl: (id: number) => authenticatedUrl(`/api/bank/statements/${id}/pdf`),
  discard: (file_ids: number[]) =>
    post<{ discarded: number }>('/api/pending/discard', { file_ids }),
  reevaluatePending: (file_ids: number[] = []) =>
    post<{ status: string; count: number | null }>('/api/pending/reevaluate', { file_ids }),
  approve: (file_ids: number[], member_id?: number) =>
    post<{ status: string; count: number }>('/api/ingest/approve', { file_ids, member_id }),
  importLocal: (body: Record<string, unknown>) =>
    post<{ status: string; files: string[] }>('/api/ingest/local', body),
  mailboxes: () =>
    get<{ mailboxes: Mailbox[]; env_configured: boolean; key_file: string }>('/api/mailboxes'),
  addMailbox: (address: string, app_password: string, member_id?: number) =>
    post<{ status: string; detail: string }>('/api/mailboxes', { address, app_password, member_id }),
  testMailbox: (id: number) =>
    post<{ status: string; detail: string }>(`/api/mailboxes/${id}/test`, {}),
  removeMailbox: (id: number) =>
    request<{ status: string }>(`/api/mailboxes/${id}`, { method: 'DELETE' }),
  assignMailboxMember: (id: number, member_id: number) =>
    request<{ status: string }>(`/api/mailboxes/${id}/member`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ member_id }),
    }),
  cards: (members: Iterable<number> = []) =>
    get<{ cards: CardRow[] }>(`/api/cards${memberQs(members)}`),
  categories: () =>
    get<{
      categories: Category[]
      majors: MajorCategory[]
      unmapped: Category[]
      provider_categories: ProviderCategory[]
    }>('/api/categories'),
  addCategory: (name: string, pattern: string | null, applies_to: string, major_id?: number | null) =>
    post<Category>('/api/categories', { name, pattern, applies_to, major_id: major_id ?? null }),
  updateCategory: (id: number, body: {
    name?: string; pattern?: string | null; applies_to?: string; clear_pattern?: boolean
  }) => request<Category>(`/api/categories/${id}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }),
  /** File one sub under a major, or pass null to send it back to unmapped. */
  linkCategory: (id: number, major_id: number | null) =>
    request<Category>(`/api/categories/${id}/major`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ major_id }),
    }),
  removeCategory: (id: number) =>
    request<{ status: string }>(`/api/categories/${id}`, { method: 'DELETE' }),
  reorderCategories: (ids: number[]) =>
    request<{ categories: Category[] }>('/api/categories/order', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids }),
    }),
  addMajorCategory: (name: string) => post<MajorCategory>('/api/major-categories', { name }),
  renameMajorCategory: (id: number, name: string) =>
    request<MajorCategory>(`/api/major-categories/${id}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }),
    }),
  removeMajorCategory: (id: number) =>
    request<{ status: string; unmapped: number }>(`/api/major-categories/${id}`, { method: 'DELETE' }),
  reapplyCategories: () => post<{ cards: number; bank: number }>('/api/categories/reapply', {}),
  updateCardTransactionCategories: (ids: number[], category: string | null) =>
    request<{
      updated: number
      rows: Pick<Txn,
        'id' | 'category' | 'derived_category' | 'category_override' | 'category_is_override'>[]
    }>('/api/transactions/category', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids, category }),
    }),
  profile: (memberId?: number) => get<Profile>(`/api/profile${memberId ? `?member_id=${memberId}` : ''}`),
  saveProfile: (full_name: string, dob: string, memberId?: number) =>
    request<Profile & { status: string }>(`/api/profile${memberId ? `?member_id=${memberId}` : ''}`, {
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
  assignCardMember: (id: number, member_id: number) =>
    request<{ status: string }>(`/api/cards/${id}/member`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ member_id }),
    }),
  assignBankAccountMember: (id: number, member_id: number) =>
    request<{ status: string }>(`/api/bank/accounts/${id}/member`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ member_id }),
    }),
}

function authenticatedUrl(url: string): string {
  const token = localStorage.getItem('sparser_token')
  if (!token) return url
  return `${url}${url.includes('?') ? '&' : '?'}access_token=${encodeURIComponent(token)}`
}

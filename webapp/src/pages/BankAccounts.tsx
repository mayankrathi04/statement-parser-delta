import { Fragment, useCallback, useEffect, useState } from 'react'
import { api, type BankAccount, type Member } from '../api'
import { seriesVar } from '../components/Charts'
import IconButton from '../components/IconButton'
import { money2 } from '../lib/format'

export default function BankAccounts({
  roster, members,
}: {
  /** Every member, so an account can be reassigned to one not currently selected. */
  roster: Member[]
  members: Set<number>
}) {
  const [accounts, setAccounts] = useState<BankAccount[]>([])
  const [open, setOpen] = useState<Record<number, boolean>>({})
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setAccounts((await api.bankBootstrap(members)).accounts)
      setError(null)
    } catch (caught) {
      setError(String((caught as Error).message))
    }
  }, [members])

  useEffect(() => { load() }, [load])
  const colourOf = (id: number) => seriesVar(
    [...accounts].sort((a, b) => a.id - b.id).findIndex((account) => account.id === id),
  )

  return (
    <>
      {error && <div className="banner" style={{ borderLeftColor: 'var(--critical)' }}>{error}</div>}
      <section className="card">
        <h2>Bank accounts</h2>
        <p className="hint">
          Accounts are discovered from imported statements. Outside the retained source PDF, only a
          one-way fingerprint and the last four digits are stored for identity and display.
        </p>
        {!accounts.length && (
          <p className="empty">
            {members.size && members.size < roster.length
              ? 'No bank accounts for the selected member. Widen the selector at the top to see the rest.'
              : 'No bank accounts yet. Upload an account statement in Bank Pipeline.'}
          </p>
        )}
        {!!accounts.length && (
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Account</th><th>Member</th><th>Number</th><th>Type</th><th>Branch</th>
                  <th style={{ textAlign: 'right' }}>Statements</th>
                  <th style={{ textAlign: 'right' }}>Txns</th><th>Coverage</th>
                </tr>
              </thead>
              <tbody>
                {accounts.map((account) => (
                  <Fragment key={account.id}>
                    <tr>
                      <td>
                        <IconButton
                          label={open[account.id] ? 'Collapse' : 'Expand'}
                          icon={open[account.id] ? 'collapse' : 'expand'}
                          title={open[account.id]
                            ? 'Hide imported statements'
                            : 'Show imported statements'}
                          onClick={() => setOpen((current) => ({
                            ...current, [account.id]: !current[account.id],
                          }))}
                        />{' '}
                        <i className="swatch" style={{ background: colourOf(account.id) }} />{' '}
                        {account.display_name}
                      </td>
                      <td><select value={account.member_id ?? ''} onChange={async (event) => {
                        await api.assignBankAccountMember(account.id, Number(event.target.value)); void load()
                      }}>{roster.map((member) => <option key={member.id} value={member.id}>{member.name}</option>)}</select></td>
                      <td><code>{account.masked_number}</code></td>
                      <td>{account.account_type ?? account.product ?? '—'}</td>
                      <td>{account.branch ?? '—'}</td>
                      <td className="num">{account.statements}</td>
                      <td className="num">{account.txn_count}</td>
                      <td>
                        {account.first_txn && account.last_txn
                          ? `${account.first_txn} → ${account.last_txn}` : '—'}
                      </td>
                    </tr>
                    {open[account.id] && (
                      <tr>
                        <td colSpan={8} style={{ background: 'var(--surface-2)' }}>
                          <div className="tile-l" style={{ marginBottom: 8 }}>
                            Statements present for this account — newest first
                          </div>
                          <table>
                            <thead>
                              <tr>
                                <th>Statement period</th><th>Rows present</th><th>File</th>
                                <th style={{ textAlign: 'right' }}>Txns</th>
                                <th style={{ textAlign: 'right' }}>Withdrawals</th>
                                <th style={{ textAlign: 'right' }}>Deposits</th>
                                <th style={{ textAlign: 'right' }}>Closing</th><th>Imported</th>
                              </tr>
                            </thead>
                            <tbody>
                              {account.history.map((statement) => (
                                <tr key={statement.id}>
                                  <td><b>{statement.period_start} → {statement.period_end}</b></td>
                                  <td>
                                    {statement.coverage_start && statement.coverage_end
                                      ? `${statement.coverage_start} → ${statement.coverage_end}` : '—'}
                                  </td>
                                  <td><code>{statement.source_file}</code></td>
                                  <td className="num">{statement.txns}</td>
                                  <td className="num">{money2(statement.withdrawals)}</td>
                                  <td className="num cre">{money2(statement.deposits)}</td>
                                  <td className="num">{money2(statement.closing_balance)}</td>
                                  <td>
                                    {statement.imported_at.replace('T', ' ')}{' '}
                                    {statement.confidence === 1 && (
                                      <span className="pill" style={{ color: 'var(--success)' }}>100%</span>
                                    )}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  )
}

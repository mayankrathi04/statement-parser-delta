export const money0 = (n: number) => '₹' + Math.round(n).toLocaleString('en-IN')

export const money2 = (n: number) =>
  '₹' + n.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

export const pct = (n: number) => `${Math.round(n * 100)}%`

/** ISO date for a month offset from an anchor. Ranges anchor to the newest
 *  transaction rather than today: the history is historical, and anchoring to
 *  now shows an empty window whenever imports lag behind the billing cycle. */
export function monthsBefore(anchor: string, months: number): string {
  const d = new Date(anchor)
  d.setMonth(d.getMonth() - months)
  return d.toISOString().slice(0, 10)
}

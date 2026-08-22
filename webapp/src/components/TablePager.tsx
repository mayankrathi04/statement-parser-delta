import type { PageSize } from '../lib/pagination'

/** The default ladder. A table whose rows are tall or whose list is short can
 *  pass its own — 25 never paginates a twenty-row list, which is exactly the
 *  case where the page is asking to be shorter. */
const DEFAULT_SIZES: PageSize[] = [25, 50, 75, 'all']

/** The footer under a paged table: what is on screen, how much fits, and the
 *  way to the next page. Shared so every table counts and pages identically. */
export default function TablePager({
  count, page, pages, size, setPage, setSize, sizes = DEFAULT_SIZES,
}: {
  count: number
  page: number
  pages: number
  size: PageSize
  setPage: (page: number) => void
  setSize: (size: PageSize) => void
  sizes?: PageSize[]
}) {
  const pageSize = size === 'all' ? Math.max(count, 1) : size
  const first = count ? page * pageSize + 1 : 0
  const last = Math.min(count, (page + 1) * pageSize)

  return (
    <div className="table-pager">
      <span className="sub">Rows {first}–{last} of {count}</span>
      <span className="spacer" />
      <label className="sub">
        Per page{' '}
        <select
          value={size}
          onChange={(e) => setSize(e.target.value === 'all' ? 'all' : Number(e.target.value))}
        >
          {sizes.map((option) => (
            <option key={option} value={option}>{option === 'all' ? 'All' : option}</option>
          ))}
        </select>
      </label>
      <button className="btn" disabled={page === 0} onClick={() => setPage(page - 1)}>← Previous</button>
      <span className="sub">Page {page + 1} of {pages}</span>
      <button className="btn" disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}>Next →</button>
    </div>
  )
}

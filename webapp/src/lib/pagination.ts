import { useEffect, useRef, useState } from 'react'

/** Rows per page. `all` means one page however long the list is. */
export type PageSize = number | 'all'

/** Put a scrolling table back at its first row.
 *
 *  The rows live in a `.tbl-scroll` box with its own scrollbar, so a new page
 *  otherwise renders under whatever offset the previous page was left at — the
 *  reader lands mid-table on rows they have not seen the top of. The box is
 *  also pulled back into view when the page has scrolled past it, which is
 *  where the pager buttons sit once a long table is open.
 */
export function scrollToFirstRow(box: HTMLElement | null): void {
  if (!box) return
  box.scrollTop = 0
  if (box.getBoundingClientRect().top < 0) {
    box.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }
}

/**
 * Paging over rows already in hand.
 *
 * `resetKey` describes what the rows are — the filters, search and sort behind
 * them. Paging restarts when that changes, but not when a category edit
 * rewrites rows already on screen, which would otherwise throw the reader back
 * to page one every time they retag a transaction.
 *
 * `scroller` belongs on the `.tbl-scroll` box holding the rows: every page
 * change scrolls it back to the first row.
 */
export function usePagination<T>(rows: T[], resetKey: unknown, initialSize: PageSize = 25) {
  const [size, setSize] = useState<PageSize>(initialSize)
  const [page, setPage] = useState(0)
  const scroller = useRef<HTMLDivElement>(null)
  const pageSize = size === 'all' ? Math.max(rows.length, 1) : size
  const pages = Math.max(1, Math.ceil(rows.length / pageSize))
  const pageIndex = Math.min(page, pages - 1)

  useEffect(() => {
    setPage(0)
    // Only the offset: a filter or sort change leaves the reader looking at the
    // same table, so pulling the page around them would be the surprise here.
    if (scroller.current) scroller.current.scrollTop = 0
  }, [resetKey, size])

  const goTo = (next: number) => {
    setPage(next)
    scrollToFirstRow(scroller.current)
  }

  return {
    rows: size === 'all' ? rows : rows.slice(pageIndex * pageSize, (pageIndex + 1) * pageSize),
    count: rows.length,
    page: pageIndex,
    pages,
    size,
    setPage: goTo,
    setSize,
    scroller,
  }
}

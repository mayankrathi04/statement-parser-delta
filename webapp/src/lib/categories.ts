import { useEffect, useMemo, useState } from 'react'
import { api, type CategoryFacet } from '../api'

/** The category filter to keep when its option list is re-read.
 *
 *  Editing a category changes what categories exist, so the filter's options
 *  are re-read after every edit. A selection that covered everything keeps
 *  covering everything — otherwise retagging a row into a brand-new category
 *  would hide the row that was just retagged. A narrower selection is left
 *  alone, minus any label that no longer describes a single row.
 */
export function reconcileSelection(
  previous: CategoryFacet[], selected: Set<string>, next: CategoryFacet[],
): Set<string> {
  if (selected.size === previous.length) return new Set(next.map((facet) => facet.name))
  const known = new Set(next.map((facet) => facet.name))
  const kept = [...selected].filter((name) => known.has(name))
  return kept.length === selected.size ? selected : new Set(kept)
}

/** Category names to offer when editing a transaction.
 *
 *  Sourced from the shared list on the Categories tab, so one added there is
 *  offered immediately — before any transaction uses it. Labels already present
 *  on rows are merged in, which covers anything typed ad hoc or left over from
 *  an issuer.
 */
export function useCategoryOptions(inUse: Iterable<string> = []): string[] {
  const [managed, setManaged] = useState<string[]>([])

  useEffect(() => {
    let live = true
    api.categories()
      .then((data) => { if (live) setManaged(data.categories.map((row) => row.name)) })
      .catch(() => undefined)
    return () => { live = false }
  }, [])

  // Collapsed to a string so a fresh array each render does not re-run the memo.
  // Newline, never a space: names like "Food & Dining" contain spaces themselves.
  const observed = [...inUse].filter(Boolean).join('\n')
  return useMemo(
    () => [...new Set([...managed, ...observed.split('\n').filter(Boolean)])]
      .sort((a, b) => a.localeCompare(b)),
    [managed, observed],
  )
}

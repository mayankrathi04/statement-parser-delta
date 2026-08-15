import type { Member } from '../api'

export type ImportTarget = {
  id: number | undefined
  name: string
  /** True when the top-bar picker named one member, so the choice is deliberate. */
  explicit: boolean
}

/** Which member a new import is filed under, derived from the shared top-bar picker.
 *
 *  A statement belongs to exactly one person, but the picker is a multi-select. So
 *  one member selected means that member; anything wider falls back to the default,
 *  and callers show which one so the attribution is never a silent guess.
 */
export function importTarget(roster: Member[], selected: Set<number>): ImportTarget {
  if (selected.size === 1) {
    const only = roster.find((member) => member.id === [...selected][0])
    if (only) return { id: only.id, name: only.name, explicit: true }
  }
  const fallback = roster.find((member) => member.is_default) ?? roster[0]
  return { id: fallback?.id, name: fallback?.name ?? 'the default member', explicit: false }
}

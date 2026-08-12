import { useEffect, useMemo, useState } from 'react'

import { listChannels, type ChannelTaxonomyEntry } from './api'

/** A `channel_key` → taxonomy-row lookup, plus whether it is still loading.
 * Every renderer that turns a `channel_key` into an operator-facing label
 * (`ArtefactBody.tsx`'s channel-plan/copy artefacts, `DistributionPack.tsx`)
 * reads the same shape, so "unresolved" and "still loading" are decided
 * identically everywhere rather than each renderer inventing its own
 * fallback. */
export interface ChannelLookup {
  get(key: string): ChannelTaxonomyEntry | undefined
  /** Every seeded/instance-added row, for a component that needs to OFFER
   * the taxonomy rather than just resolve one key already in hand — `Mint
   * Links`' channel picker (#84) is the first such caller. Same array
   * identity across renders that don't change `entries` (both come from the
   * one `useMemo` below), so a picker can depend on it without re-rendering
   * every time its parent does. */
  entries: ChannelTaxonomyEntry[]
  loading: boolean
}

/** Fetches the channel taxonomy **once** and exposes it as a lookup —
 * call this at the campaign-detail level and thread the result down to
 * every component that renders a channel, rather than each one calling
 * this hook itself. `marketing_channel` is shared, tenant-wide vocabulary
 * (not per-campaign), so one campaign's studio view needs it fetched once,
 * not once per section that happens to render a channel.
 *
 * A failed fetch resolves to an empty taxonomy rather than throwing: every
 * `channel_key` then reads as "not found" through the same unrecognised-
 * channel path callers already have to handle, which is honest — this
 * deployment's channel names genuinely can't be resolved right now — and
 * keeps a taxonomy outage from taking down the whole campaign studio.
 */
export function useChannelTaxonomy(): ChannelLookup {
  const [entries, setEntries] = useState<ChannelTaxonomyEntry[] | null>(null)

  useEffect(() => {
    let cancelled = false
    listChannels()
      .then((result) => {
        if (!cancelled) setEntries(result)
      })
      .catch(() => {
        if (!cancelled) setEntries([])
      })
    return () => {
      cancelled = true
    }
  }, [])

  // One shared `[]` for the "not loaded yet" case, not a fresh literal per
  // render — a picker keying off `entries` (e.g. in its own `useMemo`)
  // would otherwise see a "changed" array every render while still loading.
  const resolved = useMemo(() => entries ?? [], [entries])
  const byKey = useMemo(() => new Map(resolved.map((e) => [e.key, e])), [resolved])

  return useMemo(
    () => ({
      get: (key: string) => byKey.get(key),
      entries: resolved,
      loading: entries === null,
    }),
    [byKey, resolved, entries],
  )
}

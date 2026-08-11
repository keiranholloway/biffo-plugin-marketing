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

  const byKey = useMemo(() => new Map((entries ?? []).map((e) => [e.key, e])), [entries])

  return useMemo(
    () => ({
      get: (key: string) => byKey.get(key),
      loading: entries === null,
    }),
    [byKey, entries],
  )
}

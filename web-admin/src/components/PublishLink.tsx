import type { ChannelLookup } from '../lib/useChannelTaxonomy'

/** A link to where an operator actually publishes on one channel — LinkedIn's
 * post composer, Google Ads, and so on (#103b). The URL lives on the channel
 * taxonomy (`marketing_channel.publish_url`, set server-side in
 * `scripts/seed_marketing_channels.py`), not hardcoded here — this component
 * only renders whatever `channelLookup` resolves.
 *
 * Renders nothing at all when the channel has no `publish_url` (loading, an
 * unrecognised `channel_key`, or a channel that genuinely has none —
 * `trade_press_earned` is a pitch to a publication, not a composer) — never a
 * disabled or dead link, per #103b's own requirement that absence must read
 * as nothing.
 *
 * `target="_blank"` + `rel="noopener noreferrer"` (#103b): these are
 * third-party sign-in surfaces, so an unauthenticated operator bouncing
 * through that platform's own login in a new tab, without losing this pack,
 * is the correct and expected flow — this component never touches
 * credentials of its own.
 */
export function PublishLink({ channelKey, lookup }: { channelKey: string; lookup: ChannelLookup }) {
  const entry = lookup.get(channelKey)
  const url = entry?.publish_url
  if (entry === undefined || url === null || url === undefined || url === '') return null

  return (
    <a className="publish-link" href={url} target="_blank" rel="noopener noreferrer">
      Publish on {entry.label} ↗
    </a>
  )
}

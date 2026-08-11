import type { ChannelLookup } from '../lib/useChannelTaxonomy'

/** Renders a channel reference the way an operator needs to read it — never
 * the raw `channel_key` passed off as a label. #77 split `marketing_channel`
 * into a machine `key` and an operator-facing `label` specifically so
 * `google_search_paid` never has to double as "Google Search ads"; reusing
 * the key here would reintroduce that exact problem one layer up.
 *
 * Three distinct states, each visually marked so none can be mistaken for
 * another:
 *
 * - **Proposal** (`channelKey` is `null`, `suggestedLabel` is set) — a
 *   channel outside the operator's selection the channel-plan agent found
 *   strong evidence for (#67). `ChannelRecommendation` carries exactly one
 *   of `channel_key`/`suggested_label`, enforced server-side, so this is
 *   already structurally distinct from an approved-taxonomy channel — this
 *   badge makes that structural fact visible rather than inventing a new
 *   convention (#67 requires this be structural, not a rationale note).
 * - **Still loading** — the taxonomy fetch hasn't resolved yet. Shown as a
 *   neutral placeholder, not the bare key (which would look like a label
 *   for an instant) and not blank (which reads as broken).
 * - **Unrecognised** — a `channel_key` with no matching taxonomy row
 *   (deleted, or from an instance whose seed diverged). Shown as the key,
 *   clearly marked unrecognised — never blank, and never silently treated
 *   as if it were the label.
 */
export function ChannelName({
  channelKey,
  suggestedLabel,
  lookup,
}: {
  channelKey: string | null
  suggestedLabel: string | null
  lookup: ChannelLookup
}) {
  if (channelKey === null) {
    return (
      <span className="channel-name channel-proposed">
        <span className="badge badge-proposed">Proposed — outside selection</span>{' '}
        {suggestedLabel !== null && suggestedLabel !== '' ? suggestedLabel : '(no label given)'}
      </span>
    )
  }

  if (lookup.loading) {
    return (
      <span className="channel-name channel-loading" aria-live="polite">
        Loading channel…
      </span>
    )
  }

  const entry = lookup.get(channelKey)
  if (entry === undefined) {
    return (
      <span className="channel-name channel-unknown" title={`No taxonomy entry for channel_key "${channelKey}"`}>
        {channelKey} <span className="badge badge-unknown">unrecognised channel</span>
      </span>
    )
  }

  return <span className="channel-name">{entry.label}</span>
}

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
 * - **Proposal** — a channel outside the operator's selection the
 *   channel-plan agent found strong evidence for (#67), badged so it cannot
 *   be read as part of the plan.
 * - **Still loading** — the taxonomy fetch hasn't resolved yet. Shown as a
 *   neutral placeholder, not the bare key (which would look like a label
 *   for an instant) and not blank (which reads as broken).
 * - **Unrecognised** — a `channel_key` with no matching taxonomy row
 *   (deleted, or from an instance whose seed diverged). Shown as the key,
 *   clearly marked unrecognised — never blank, and never silently treated
 *   as if it were the label.
 *
 * ## `proposed` is told, not inferred — and that is the #67 fix
 *
 * This component used to derive "is a proposal" from `channelKey === null`,
 * which was sound while a proposal could only ever be free text. Since #67's
 * third increment the agent may propose a **taxonomy** channel the operator
 * deselected, and that carries a perfectly real `channel_key` — so inferring
 * from the key would render it as an ordinary planned channel, with its
 * proper taxonomy label and no badge, which is precisely the
 * "nothing downstream can mistake a proposal for an approved channel"
 * requirement being broken in the one place an operator would actually see
 * it. The caller knows which list the entry came out of; it says so.
 *
 * A null `channelKey` is still treated as a proposal regardless of the flag:
 * every channel-plan body written before that increment carries its proposals
 * inside `channels` with no key, and those must keep their badge.
 */
export function ChannelName({
  channelKey,
  suggestedLabel,
  proposed = false,
  lookup,
}: {
  channelKey: string | null
  suggestedLabel: string | null
  /** True when this entry came out of `ChannelPlanBody.proposals`. Defaults
   * to `false` for the render sites that have no proposals to show at all —
   * copy and the distribution pack, whose entries are approved channels by
   * construction. */
  proposed?: boolean
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

  if (proposed) {
    // A real taxonomy row, so it gets its real label — the badge is what
    // says it is not in the plan. Rendering the key instead would be the
    // #77 mistake (a machine key passed off as a label) reintroduced.
    return (
      <span className="channel-name channel-proposed">
        <span className="badge badge-proposed">Proposed — outside selection</span>{' '}
        {lookup.loading ? 'Loading channel…' : (lookup.get(channelKey)?.label ?? channelKey)}
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

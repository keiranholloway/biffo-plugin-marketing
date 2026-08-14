import { useMemo, useState } from 'react'

import { updateCampaign, type Campaign, type CampaignMotion } from '../lib/api'
import {
  CAMPAIGN_MOTIONS,
  channelMotionsFor,
  MOTION_LABELS,
  targetChannelKeys,
} from '../lib/campaignTargeting'
import type { ChannelLookup } from '../lib/useChannelTaxonomy'

/** Category slug → the heading an operator reads. The slugs are the seeded
 * taxonomy's own (`scripts/seed_marketing_channels.py`'s `CATEGORIES`); an
 * instance-added channel may carry a category this map has never heard of,
 * which is why the fallback below humanises rather than dropping the group —
 * the whole point of the taxonomy living in a table is that an instance can
 * extend it without a plugin release. */
const CATEGORY_LABELS: Record<string, string> = {
  search: 'Search',
  social: 'Social',
  video: 'Video',
  email: 'Email',
  content: 'Content',
  communities: 'Communities',
  partner: 'Partner / affiliate',
  events: 'Events',
  trade_press: 'Trade press',
  direct_mail: 'Direct mail',
  local_field: 'Local / field',
}

function categoryLabel(category: string): string {
  return (
    CATEGORY_LABELS[category] ??
    category.replace(/_/g, ' ').replace(/^./, (first) => first.toUpperCase())
  )
}

/** The two decisions #67 moves ahead of the channel-plan agent: this
 * campaign's **motion**, and the **channels** it may plan against.
 *
 * Before this, the agent chose both, and the operator's first involvement was
 * approving or rejecting a finished plan — so a campaign that could only ever
 * execute organically got a plan half-composed of ad platforms nobody was
 * going to buy, and the only remedy was to reject the whole run.
 *
 * ## This picker is not the constraint
 *
 * `start_channel_plan_route` narrows the taxonomy the agent is shown to
 * exactly `selection ∩ motion`, and `pipeline.extract_channel_plan` re-checks
 * what comes back. Nothing here is trusted by the server: a stale page can
 * show the wrong options, never widen what a run may return. What this
 * component owes the operator is that the choice is *possible* and that its
 * consequences are visible — which is why narrowing the motion says what it
 * will drop instead of quietly discarding it.
 */
export function CampaignTargeting({
  campaign,
  channelLookup,
  onCampaignUpdated,
}: {
  campaign: Campaign
  channelLookup: ChannelLookup
  onCampaignUpdated: (campaign: Campaign) => void
}) {
  const [motion, setMotion] = useState<CampaignMotion | null>(campaign.motion ?? null)
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(targetChannelKeys(campaign)),
  )
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const allowedMotions = useMemo(
    () => (motion === null ? new Set<string>() : channelMotionsFor(motion)),
    [motion],
  )

  // Offered in taxonomy order, grouped by category. Order comes from the
  // taxonomy rather than from the order the operator clicked, so what gets
  // saved is stable and re-reading a campaign shows the same list twice.
  const offered = useMemo(
    () => channelLookup.entries.filter((entry) => allowedMotions.has(entry.motion)),
    [channelLookup.entries, allowedMotions],
  )

  const grouped = useMemo(() => {
    const groups = new Map<string, typeof offered>()
    for (const entry of offered) {
      const existing = groups.get(entry.category)
      if (existing === undefined) groups.set(entry.category, [entry])
      else existing.push(entry)
    }
    return [...groups.entries()]
  }, [offered])

  /** What will actually be saved: the selection, narrowed to what this motion
   * can run. */
  const keepable = offered.filter((entry) => selected.has(entry.key)).map((entry) => entry.key)
  // Selected keys this motion excludes, or that are no longer in the taxonomy
  // at all. Counted rather than silently dropped — the campaign still holds
  // them until this form is saved, and an operator who narrows a motion by
  // accident should be able to see what it cost before committing.
  const droppedCount = selected.size - keepable.length

  function toggle(key: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  async function save(event: React.FormEvent) {
    event.preventDefault()
    if (motion === null || keepable.length === 0) return
    setSaving(true)
    setError(null)
    try {
      const updated = await updateCampaign(campaign.id, {
        motion,
        target_channel_keys: keepable.join(','),
      })
      setSelected(new Set(keepable))
      onCampaignUpdated(updated)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="targeting" aria-label="Campaign targeting">
      <h3>Motion and target channels</h3>
      <p className="hint">
        How this campaign reaches people, and which channels it may plan against. Both are
        required before the channel plan can run — the agent plans within this selection rather
        than choosing the channel set for you. It may still propose a channel outside it where
        its evidence is strong; a proposal is flagged as such and needs your acceptance before
        anything is written for it.
      </p>

      <form onSubmit={save}>
        <fieldset className="motion">
          <legend>Motion</legend>
          {CAMPAIGN_MOTIONS.map((value) => (
            <label key={value} htmlFor={`motion-${value}`}>
              <input
                type="radio"
                id={`motion-${value}`}
                name="motion"
                value={value}
                checked={motion === value}
                onChange={() => setMotion(value)}
              />
              {MOTION_LABELS[value]}
            </label>
          ))}
        </fieldset>

        {motion === null && (
          <p className="empty">
            Choose a motion first — it decides which channels are even eligible.
          </p>
        )}

        {motion !== null && channelLookup.loading && <p className="empty">Loading channels…</p>}

        {motion !== null && !channelLookup.loading && offered.length === 0 && (
          <p className="empty">
            No {motion === 'both' ? '' : `${motion} `}channels are available on this platform yet.
            An admin seeds the channel taxonomy.
          </p>
        )}

        {grouped.map(([category, entries]) => (
          <fieldset key={category} className="channel-group">
            <legend>{categoryLabel(category)}</legend>
            {entries.map((entry) => (
              <label key={entry.key} htmlFor={`channel-${entry.key}`}>
                <input
                  type="checkbox"
                  id={`channel-${entry.key}`}
                  checked={selected.has(entry.key)}
                  onChange={() => toggle(entry.key)}
                />
                {entry.label}
              </label>
            ))}
          </fieldset>
        ))}

        {droppedCount > 0 && (
          <p className="hint">
            {droppedCount} selected channel{droppedCount === 1 ? '' : 's'}{' '}
            {droppedCount === 1 ? 'is' : 'are'} outside this motion and will be dropped when you
            save.
          </p>
        )}

        <button type="submit" disabled={saving || motion === null || keepable.length === 0}>
          {saving ? 'Saving…' : 'Save targeting'}
        </button>
        {motion !== null && keepable.length === 0 && !channelLookup.loading && (
          <p className="hint">Select at least one channel.</p>
        )}
        {error !== null && <p className="error">{error}</p>}
      </form>
    </section>
  )
}

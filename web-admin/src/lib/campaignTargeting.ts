import type { Campaign, CampaignMotion } from './api'

/** The three campaign motions, in the order an operator reads them (#67).
 * Mirrors `definitions.CAMPAIGN_MOTIONS` server-side — the server is the
 * authority, and refuses anything outside this set, so this list exists to
 * render the choice, never to decide what is valid. */
export const CAMPAIGN_MOTIONS: readonly CampaignMotion[] = ['organic', 'paid', 'both']

export const MOTION_LABELS: Record<CampaignMotion, string> = {
  organic: 'Organic',
  paid: 'Paid',
  both: 'Both',
}

/** The `marketing_channel.motion` values a campaign of this motion may run —
 * the browser-side twin of `definitions.motions_allowed_by`. Used to narrow
 * what the picker OFFERS; it is not the enforcement (the plan route filters
 * the agent's taxonomy and the extractor re-checks the result), so a stale
 * page can only ever show the wrong options, never smuggle a channel past
 * the constraint. */
export function channelMotionsFor(motion: CampaignMotion): ReadonlySet<string> {
  return motion === 'both' ? new Set(['organic', 'paid']) : new Set([motion])
}

/** A campaign's selected channel keys, parsed from its one comma-separated
 * column. Blank entries are dropped, so `''`, `null` and `'a,,b'` all behave
 * the way a reader expects rather than yielding empty keys nothing matches. */
export function targetChannelKeys(campaign: Campaign): string[] {
  return (campaign.target_channel_keys ?? '')
    .split(',')
    .map((key) => key.trim())
    .filter((key) => key !== '')
}

/** Whether this campaign has BOTH of the operator decisions the channel-plan
 * stage requires (#67). The stage's own route 422s without them, so this is
 * what stops the UI offering a button whose only outcome is that error.
 *
 * Deliberately not "motion or channels": each without the other is a campaign
 * that cannot plan, and reporting it as ready would move the failure from a
 * disabled button to a red error after a click. */
export function hasTargeting(campaign: Campaign): boolean {
  return (
    campaign.motion != null &&
    CAMPAIGN_MOTIONS.includes(campaign.motion) &&
    targetChannelKeys(campaign).length > 0
  )
}

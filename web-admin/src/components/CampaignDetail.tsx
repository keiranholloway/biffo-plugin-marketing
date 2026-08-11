import { useState } from 'react'

import { updateCampaign, type Campaign } from '../lib/api'
import { useChannelTaxonomy } from '../lib/useChannelTaxonomy'
import { DistributionPack } from './DistributionPack'
import { ImageGenerator } from './ImageGenerator'
import { PaidPack } from './PaidPack'
import { Pipeline } from './Pipeline'
import { Results } from './Results'

/** One campaign's whole studio: the brief, the pipeline gates, image
 * generation, both distribution packs, and results — everything M3–M9 built
 * an API for but that, before this component, had no browser surface at all.
 */
export function CampaignDetail({
  campaign,
  onBack,
  onCampaignUpdated,
}: {
  campaign: Campaign
  onBack: () => void
  onCampaignUpdated: (campaign: Campaign) => void
}) {
  const [brief, setBrief] = useState(campaign.brief ?? '')
  const [savingBrief, setSavingBrief] = useState(false)
  const [briefError, setBriefError] = useState<string | null>(null)

  // Fetched once here, not by `Pipeline`/`DistributionPack` themselves —
  // `marketing_channel` is shared tenant-wide vocabulary, so one request
  // per campaign view is correct; one per section that renders a channel
  // would not be.
  const channelLookup = useChannelTaxonomy()

  async function saveBrief(event: React.FormEvent) {
    event.preventDefault()
    setSavingBrief(true)
    setBriefError(null)
    try {
      const updated = await updateCampaign(campaign.id, { brief: brief.trim() })
      onCampaignUpdated(updated)
    } catch (e: unknown) {
      setBriefError(e instanceof Error ? e.message : String(e))
    } finally {
      setSavingBrief(false)
    }
  }

  const campaignBrief = (campaign.brief ?? '').trim()
  const hasBrief = campaignBrief !== ''

  return (
    <div className="campaign-detail">
      <button type="button" className="back" onClick={onBack}>
        &larr; Back to campaigns
      </button>
      <h2>{campaign.name}</h2>
      <p className="lede">{campaign.destination_url ?? 'No destination URL set'}</p>

      <section className="brief" aria-label="Campaign brief">
        <h3>Brief</h3>
        <p className="hint">
          What research and positioning are grounded in. Required before research can start —
          nothing else in the pipeline can run before it does.
        </p>
        <form onSubmit={saveBrief}>
          <label htmlFor="brief">Brief</label>
          <textarea
            id="brief"
            value={brief}
            onChange={(e) => setBrief(e.target.value)}
            placeholder="Who this campaign is for, what it's promoting, and why now."
          />
          <button type="submit" disabled={savingBrief || brief.trim() === campaignBrief}>
            {savingBrief ? 'Saving…' : 'Save brief'}
          </button>
          {briefError !== null && <p className="error">{briefError}</p>}
        </form>
      </section>

      <h3>Pipeline</h3>
      <Pipeline campaignId={campaign.id} hasBrief={hasBrief} channelLookup={channelLookup} />

      <h3>Images</h3>
      <ImageGenerator campaignId={campaign.id} />

      <h3>Distribution pack</h3>
      <DistributionPack campaignId={campaign.id} channelLookup={channelLookup} />

      <h3>Paid brief pack</h3>
      <PaidPack campaignId={campaign.id} />

      <h3>Results</h3>
      <Results campaignId={campaign.id} />
    </div>
  )
}

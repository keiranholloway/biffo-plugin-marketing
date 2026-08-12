import { useState } from 'react'

import { getPack, type Pack } from '../lib/api'
import { composeChannelCopy } from '../lib/packCopy'
import { useClipboard } from '../lib/useClipboard'
import type { ChannelLookup } from '../lib/useChannelTaxonomy'
import { ChannelName } from './ChannelName'
import { CopyButton } from './CopyButton'
import { MissingPlacementsWarning, PackAssets, PackGuidance, PackLinks } from './PackParts'
import { PublishLink } from './PublishLink'

/** The distribution pack (M5): assets, approved copy, tracked links and
 * guidance, assembled from an **approved** copy artefact — `pack_routes.py`'s
 * own "the milestone's actual deliverable" (M1–M4 only produce an approved
 * plan; nothing an operator can publish comes out of them without this).
 *
 * `missing_placements` is shown, not hidden: this plugin only renders a
 * campaign's source creative today, not per-placement crops (issue #36), and
 * a pack that quietly omitted that gap would look complete when it is not.
 *
 * `pack.copy` is the approved `CopySetBody.channels` shape verbatim (#76
 * increment 2: `channel_key`, not a label) — `channelLookup` (fetched once
 * by `CampaignDetail` via `useChannelTaxonomy`, not by this component) is
 * what turns that key into the label an operator can actually read.
 */
export function DistributionPack({
  campaignId,
  campaignName,
  channelLookup,
}: {
  campaignId: string
  /** Only used to name a downloaded creative (#117) — object storage knows
   * the bytes as a uuid, so the pack has to supply the human name. Threaded
   * from `CampaignDetail`, which already holds the campaign row, rather than
   * refetched here. */
  campaignName: string
  channelLookup: ChannelLookup
}) {
  const [pack, setPack] = useState<Pack | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { copied, copy, error: copyError } = useClipboard()

  async function load() {
    setLoading(true)
    setError(null)
    try {
      setPack(await getPack(campaignId))
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="pack" aria-label="Distribution pack">
      <button type="button" onClick={load} disabled={loading}>
        {loading ? 'Loading…' : pack === null ? 'Load pack' : 'Reload pack'}
      </button>

      {error !== null && <p className="error">{error}</p>}
      {copyError !== null && <p className="error">{copyError}</p>}

      {pack !== null && (
        <div className="pack-body">
          <MissingPlacementsWarning missingPlacements={pack.missing_placements} />

          <PackAssets assets={pack.assets} campaignName={campaignName} />

          <h4>Copy</h4>
          {pack.copy.length === 0 && <p className="empty">No approved copy channels.</p>}
          <ul className="copy-list">
            {pack.copy.map((c, i) => {
              const link = pack.links.find((l) => l.channel === c.channel_key) ?? null
              const allText = composeChannelCopy({
                headline: c.headline,
                body: c.body,
                cta: c.cta,
                linkUrl: link?.url ?? null,
              })
              return (
                <li key={`${c.channel_key}-${i}`}>
                  <div className="copy-list-head">
                    <strong>
                      <ChannelName channelKey={c.channel_key} suggestedLabel={null} lookup={channelLookup} />
                    </strong>{' '}
                    <span className={`motion motion-${c.motion}`}>{c.motion}</span>
                    <PublishLink channelKey={c.channel_key} lookup={channelLookup} />
                  </div>
                  <p className="headline copy-field">
                    <span>{c.headline}</span>
                    <CopyButton text={c.headline} label="Copy headline" copied={copied} onCopy={copy} />
                  </p>
                  <p className="copy-field">
                    <span>{c.body}</span>
                    <CopyButton text={c.body} label="Copy body" copied={copied} onCopy={copy} />
                  </p>
                  <p className="cta copy-field">
                    <span>{c.cta}</span>
                    <CopyButton text={c.cta} label="Copy CTA" copied={copied} onCopy={copy} />
                  </p>
                  <div className="copy-actions">
                    <CopyButton
                      text={allText}
                      label="Copy all for this channel"
                      copiedLabel="Copied all"
                      copied={copied}
                      onCopy={copy}
                      className="copy-all"
                    />
                  </div>
                </li>
              )
            })}
          </ul>

          <PackLinks links={pack.links} copied={copied} onCopy={copy} />

          <PackGuidance guidance={pack.guidance} />
        </div>
      )}
    </section>
  )
}

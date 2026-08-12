import { useState } from 'react'

import { getPaidPack, type PaidPack as PaidPackData } from '../lib/api'
import { composeChannelCopy } from '../lib/packCopy'
import { useClipboard } from '../lib/useClipboard'
import type { ChannelLookup } from '../lib/useChannelTaxonomy'
import { CopyButton } from './CopyButton'
import { MissingPlacementsWarning, PackAssets, PackGuidance, PackLinks } from './PackParts'
import { PublishLink } from './PublishLink'

/** The paid brief pack (M9): ad copy at real platform character limits,
 * creative, targeting drawn from the approved positioning, a declared budget
 * heuristic, tracked links, and spend — reported as explicitly unmeasurable
 * (issue #31), reusing the exact same `UnmeasuredMetric` shape the results
 * dashboard uses, so "no data" reads the same way in both places rather than
 * a bespoke zero here.
 *
 * Assets, tracked links, the missing-placements warning and guidance are the
 * same pieces `DistributionPack` renders — shared via `PackParts.tsx` rather
 * than duplicated (`paid_pack_routes.py`'s own module docstring: "this is
 * the organic pack's shape, with paid-specific additions, not a second
 * assembler").
 *
 * `channelLookup` (fetched once by `CampaignDetail` via `useChannelTaxonomy`,
 * threaded down the same way `DistributionPack` already receives it) is what
 * resolves each ad-copy row's `channel` — a real taxonomy `channel_key`,
 * per `paid_pack_routes._ad_copy_variant` — into its `publish_url` (#103b).
 */
export function PaidPack({
  campaignId,
  channelLookup,
}: {
  campaignId: string
  channelLookup: ChannelLookup
}) {
  const [pack, setPack] = useState<PaidPackData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { copied, copy, error: copyError } = useClipboard()

  async function load() {
    setLoading(true)
    setError(null)
    try {
      setPack(await getPaidPack(campaignId))
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="paid-pack" aria-label="Paid brief pack">
      <button type="button" onClick={load} disabled={loading}>
        {loading ? 'Loading…' : pack === null ? 'Load paid pack' : 'Reload paid pack'}
      </button>

      {error !== null && <p className="error">{error}</p>}
      {copyError !== null && <p className="error">{copyError}</p>}

      {pack !== null && (
        <div className="pack-body">
          <MissingPlacementsWarning missingPlacements={pack.missing_placements} />

          <h4>Ad copy</h4>
          <ul className="ad-copy">
            {pack.ad_copy.map((c, i) => {
              const link = pack.links.find((l) => l.channel === c.channel) ?? null
              // The server never returns the untrimmed original alongside the
              // fitted copy (`_ad_copy_variant`'s own docstring: a second,
              // longer version sitting next to the one that actually fits is
              // an invitation to paste the wrong one) — so the fitted text,
              // ellipsis included, is the only thing there is to copy, and
              // the only thing "copy all" can compose from.
              const allText = composeChannelCopy({
                headline: c.headline,
                body: c.body,
                cta: c.cta,
                linkUrl: link?.url ?? null,
              })
              const anyTruncated = c.headline_truncated || c.body_truncated || c.cta_truncated
              return (
                <li key={`${c.channel}-${i}`}>
                  <div className="copy-list-head">
                    <strong>{c.channel}</strong> <span className="platform">{c.platform}</span>
                    <PublishLink channelKey={c.channel} lookup={channelLookup} />
                  </div>
                  <p className="headline copy-field">
                    <span>
                      {c.headline}
                      {c.headline_truncated && (
                        <span className="trimmed"> (trimmed to {c.headline_limit} chars)</span>
                      )}
                    </span>
                    <CopyButton
                      text={c.headline}
                      label={c.headline_truncated ? 'Copy headline (trimmed)' : 'Copy headline'}
                      copied={copied}
                      onCopy={copy}
                    />
                  </p>
                  <p className="copy-field">
                    <span>
                      {c.body}
                      {c.body_truncated && <span className="trimmed"> (trimmed to {c.body_limit} chars)</span>}
                    </span>
                    <CopyButton
                      text={c.body}
                      label={c.body_truncated ? 'Copy body (trimmed)' : 'Copy body'}
                      copied={copied}
                      onCopy={copy}
                    />
                  </p>
                  <p className="cta copy-field">
                    <span>
                      {c.cta}
                      {c.cta_truncated && <span className="trimmed"> (trimmed to {c.cta_limit} chars)</span>}
                    </span>
                    <CopyButton
                      text={c.cta}
                      label={c.cta_truncated ? 'Copy CTA (trimmed)' : 'Copy CTA'}
                      copied={copied}
                      onCopy={copy}
                    />
                  </p>
                  <div className="copy-actions">
                    <CopyButton
                      text={allText}
                      label={
                        anyTruncated
                          ? 'Copy all for this channel (includes trimmed copy)'
                          : 'Copy all for this channel'
                      }
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

          <h4>Targeting</h4>
          {pack.targeting.length === 0 && <p className="empty">No segments to target.</p>}
          <ul className="targeting">
            {pack.targeting.map((s, i) => (
              <li key={`${s.name}-${i}`}>
                <strong>{s.name}</strong>
                <p>{s.description}</p>
                <p className="hint">
                  Grounded in {s.source_count} research source{s.source_count === 1 ? '' : 's'} — see the
                  positioning artefact for the full citations.
                </p>
              </li>
            ))}
          </ul>

          <h4>Budget</h4>
          <p>
            {pack.budget.channel_count} paid channel(s) · ${pack.budget.per_channel_daily.toFixed(0)}/day
            each · ${pack.budget.total_daily.toFixed(0)}/day total over {pack.budget.test_window_days} days
            (${pack.budget.total_test_budget.toFixed(0)} total)
          </p>
          <p className="hint">{pack.budget.basis}</p>

          <h4>Spend</h4>
          <p className="unmeasurable">Not measurable — {pack.spend.reason}</p>

          <PackAssets assets={pack.assets} />

          <PackLinks links={pack.links} copied={copied} onCopy={copy} />

          <PackGuidance guidance={pack.guidance} />
        </div>
      )}
    </section>
  )
}

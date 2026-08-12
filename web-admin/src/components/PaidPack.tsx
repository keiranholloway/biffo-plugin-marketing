import { useState } from 'react'

import { getPaidPack, type PaidPack as PaidPackData } from '../lib/api'
import { useClipboard } from '../lib/useClipboard'
import { MissingPlacementsWarning, PackAssets, PackGuidance, PackLinks } from './PackParts'

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
 */
export function PaidPack({ campaignId }: { campaignId: string }) {
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
            {pack.ad_copy.map((c, i) => (
              <li key={`${c.channel}-${i}`}>
                <strong>{c.channel}</strong> <span className="platform">{c.platform}</span>
                <p className="headline">
                  {c.headline}
                  {c.headline_truncated && (
                    <span className="trimmed"> (trimmed to {c.headline_limit} chars)</span>
                  )}
                </p>
                <p>
                  {c.body}
                  {c.body_truncated && <span className="trimmed"> (trimmed to {c.body_limit} chars)</span>}
                </p>
                <p className="cta">
                  {c.cta}
                  {c.cta_truncated && <span className="trimmed"> (trimmed to {c.cta_limit} chars)</span>}
                </p>
              </li>
            ))}
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

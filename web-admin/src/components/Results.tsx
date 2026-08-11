import { useEffect, useState } from 'react'

import { getResults, type CampaignResults, type UnmeasuredMetric } from '../lib/api'

/** One unmeasurable metric, rendered so it can never be mistaken for a zero.
 * `results_routes.py`'s own discipline: "no data" is a structurally distinct
 * thing from "zero" — a confident number that is wrong in a systematic
 * direction is worse than a missing one. Every share carries its
 * denominator, when one is known here. */
function UnmeasuredCell({ label, metric }: { label: string; metric: UnmeasuredMetric }) {
  return (
    <p className="unmeasurable">
      <strong>{label}:</strong> not measurable — {metric.reason}
      {metric.denominator !== null && <> (denominator: {metric.denominator})</>}
    </p>
  )
}

/** The results dashboard (M8) for one campaign. `/results` itself is global —
 * every campaign in the tenant, in one call — so this filters client-side to
 * the campaign this detail view is showing, rather than adding a
 * per-campaign route that does not exist server-side.
 *
 * Clicks are real, measured rows. Leads, conversions and cost are not — this
 * plugin has no reachable transport to `demo_requests`/`lead_source_costs`
 * (issue #31) — and are rendered as explicitly unmeasurable, never as a
 * silent zero.
 */
export function Results({ campaignId }: { campaignId: string }) {
  const [results, setResults] = useState<CampaignResults | null>(null)
  const [found, setFound] = useState(true)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    getResults()
      .then((all) => {
        if (cancelled) return
        const mine = all.campaigns.find((c) => c.campaign_id === campaignId) ?? null
        setResults(mine)
        setFound(mine !== null)
      })
      .catch((e: unknown) => {
        if (cancelled) return
        setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [campaignId])

  if (loading) return <p className="empty">Loading…</p>
  if (error !== null) return <p className="error">Could not load results: {error}</p>
  if (!found || results === null) {
    return <p className="empty">No results yet for this campaign.</p>
  }

  return (
    <section className="results" aria-label="Results">
      <h4>Clicks</h4>
      <p>
        {results.clicks.total} total — {results.clicks.paid} paid, {results.clicks.organic} organic
        {results.clicks.unknown_channel_type > 0 && (
          <>, {results.clicks.unknown_channel_type} of unknown channel type</>
        )}
      </p>

      <UnmeasuredCell label="Leads" metric={results.leads} />
      <UnmeasuredCell label="Conversions" metric={results.conversions} />
      <UnmeasuredCell label="Cost" metric={results.cost} />
    </section>
  )
}

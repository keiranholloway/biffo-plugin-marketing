import { useEffect, useState } from 'react'

import { getResults, type CampaignResults, type Metric } from '../lib/api'

/** One leads/conversions/cost figure — measured, or explicitly not.
 *
 * `results_routes.py`'s own discipline: "no data" is a structurally distinct
 * thing from "zero" — a confident number that is wrong in a systematic
 * direction is worse than a missing one. So the two branches read
 * differently on purpose, and a measured `0` renders as `0` rather than
 * borrowing the unmeasurable wording.
 *
 * The denominator is printed the same way in **both** branches. Showing it
 * only when a metric was unmeasurable (the pre-#114 behaviour) inverted the
 * requirement: the population vanished at exactly the moment there was a
 * share to state it for. */
function MetricCell({ label, metric }: { label: string; metric: Metric }) {
  const denominator = metric.denominator !== null && <> (denominator: {metric.denominator})</>

  if (metric.measurable) {
    return (
      <p className="measured">
        <strong>{label}:</strong> {metric.value}
        {denominator}
      </p>
    )
  }

  return (
    <p className="unmeasurable">
      <strong>{label}:</strong> not measurable — {metric.reason}
      {denominator}
    </p>
  )
}

/** The results dashboard (M8) for one campaign. `/results` itself is global —
 * every campaign in the tenant, in one call — so this filters client-side to
 * the campaign this detail view is showing, rather than adding a
 * per-campaign route that does not exist server-side.
 *
 * Clicks are real, measured rows. Leads, conversions and cost come from the
 * instance-configured leads source (issue #31): a real figure where that
 * source answered, and an explicit reason where it could not — never a
 * silent zero, and never a measured figure disguised as unmeasurable.
 *
 * `unattributed_clicks` is tenant-wide rather than per-campaign, so it is
 * rendered once for the whole section: it is the count excluded from every
 * campaign's total above, and stating it is what stops a share here being
 * computed over a filtered population and read as if it covered the whole
 * one.
 */
export function Results({ campaignId }: { campaignId: string }) {
  const [results, setResults] = useState<CampaignResults | null>(null)
  const [unattributed, setUnattributed] = useState(0)
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
        setUnattributed(all.unattributed_clicks)
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

      <MetricCell label="Leads" metric={results.leads} />
      <MetricCell label="Conversions" metric={results.conversions} />
      <MetricCell label="Cost" metric={results.cost} />

      <p className="excluded">
        {unattributed} click{unattributed === 1 ? '' : 's'} could not be attributed to any campaign
        {unattributed > 0 ? ' — excluded from every campaign total above.' : '.'}
      </p>
    </section>
  )
}

import { useState } from 'react'

import { getPaidPack, recordSpend, type PaidPack as PaidPackData, type SpendMetric } from '../lib/api'
import { composeChannelCopy } from '../lib/packCopy'
import { useClipboard } from '../lib/useClipboard'
import type { ChannelLookup } from '../lib/useChannelTaxonomy'
import { CopyButton } from './CopyButton'
import { MissingPlacementsWarning, PackAssets, PackGuidance, PackLinks } from './PackParts'
import { PublishLink } from './PublishLink'

/** One recorded-spend figure, or the reason there isn't one.
 *
 * Deliberately the same two-branch shape as `Results.tsx`'s `MetricCell`, and
 * for the same reason: "no data" is a structurally distinct thing from
 * "zero", so a measured `0` renders as `0` rather than borrowing the
 * unmeasurable wording. Getting this wrong in the other direction is issue
 * #114 — a real figure rendered as "not measurable — undefined" because only
 * the unmeasurable branch was representable.
 *
 * Not shared with `MetricCell` itself: this one is money, so it prints the
 * currency the value is denominated in, and it carries no denominator (a
 * spend is an amount, not a share of anything). */
function SpendFigure({ spend }: { spend: SpendMetric }) {
  if (spend.measurable) {
    return (
      <p className="measured">
        {spend.currency} {spend.value}
      </p>
    )
  }
  return <p className="unmeasurable">Not measurable — {spend.reason}</p>
}

/** The form an operator records spend with (issue #8).
 *
 * Deliberately minimal — an amount, a currency and an optional note. This
 * plugin calls no ad platform API, so the only way a spend figure can ever
 * exist is for the person who spent it to type it in; anything more elaborate
 * than that is a reporting feature nothing yet reads.
 *
 * Entries are additive (`spend_routes.record_spend_route`): the pack totals
 * every one. There is no edit or delete here, because the server declares no
 * route for either yet. */
function RecordSpend({ campaignId, onRecorded }: { campaignId: string; onRecorded: () => Promise<void> }) {
  const [amount, setAmount] = useState('')
  const [currency, setCurrency] = useState('USD')
  const [notes, setNotes] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    const parsed = Number(amount)
    if (amount.trim() === '' || Number.isNaN(parsed) || parsed < 0) {
      // Caught here rather than sent, so the operator gets the reason back
      // immediately instead of a 422 that says the same thing more slowly.
      setError('Enter the amount spent as a number of 0 or more.')
      return
    }
    setSaving(true)
    setError(null)
    try {
      await recordSpend(campaignId, { amount: parsed, currency, notes: notes.trim() || null })
      setAmount('')
      setNotes('')
      // Reload rather than patching the figure locally: the total is the
      // server's to compute (it may refuse to sum mixed currencies), and a
      // number this component worked out itself could disagree with it.
      await onRecorded()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <form className="record-spend" onSubmit={submit}>
      <label htmlFor="spend-amount">Amount spent</label>
      <input
        id="spend-amount"
        type="number"
        min="0"
        step="0.01"
        value={amount}
        onChange={(e) => setAmount(e.target.value)}
      />
      <label htmlFor="spend-currency">Currency</label>
      <input
        id="spend-currency"
        type="text"
        maxLength={3}
        value={currency}
        onChange={(e) => setCurrency(e.target.value)}
      />
      <label htmlFor="spend-notes">Note (optional)</label>
      <input id="spend-notes" type="text" value={notes} onChange={(e) => setNotes(e.target.value)} />
      <button type="submit" disabled={saving}>
        {saving ? 'Recording…' : 'Record spend'}
      </button>
      {error !== null && <p className="error">{error}</p>}
    </form>
  )
}

/** The paid brief pack (M9): ad copy at real platform character limits,
 * creative, targeting drawn from the approved positioning, a declared budget
 * heuristic, tracked links, and spend — which since issue #8 is a real figure
 * an operator recorded, not a permanently unmeasurable field.
 *
 * A campaign with nothing recorded is still explicitly unmeasurable rather
 * than a zero, using the same wording discipline the results dashboard uses,
 * so "no data" reads the same way in both places. See `spend_routes.py`'s
 * module docstring for why "nobody has told us" is not "nothing was spent".
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
  campaignName,
  channelLookup,
}: {
  campaignId: string
  /** Only used to name a downloaded creative (#117) — see
   * `DistributionPack`'s own copy of this prop. */
  campaignName: string
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
          <SpendFigure spend={pack.spend} />
          <RecordSpend campaignId={campaignId} onRecorded={load} />

          <PackAssets assets={pack.assets} campaignName={campaignName} />

          <PackLinks links={pack.links} copied={copied} onCopy={copy} />

          <PackGuidance guidance={pack.guidance} />
        </div>
      )}
    </section>
  )
}

import { useCallback, useEffect, useState } from 'react'

import {
  generateStill,
  getCampaignAssets,
  type GenerateStillResponse,
  type PackAsset,
} from '../lib/api'

/** `cost_usd` is nullable and the null case is load-bearing (`ledger.py` /
 * `image_routes.py`): show "unpriced", never £0 — a £0 reads as "this cost
 * nothing", which is a different and false claim from "this deployment could
 * not price it". */
function formatCost(ledger: GenerateStillResponse['ledger']): string {
  if (ledger.unpriced || ledger.cost_usd === null) return 'unpriced'
  return `$${ledger.cost_usd.toFixed(4)}`
}

function assetLabel(asset: PackAsset): string {
  if (asset.is_source === true) return 'Source creative'
  return asset.placement ?? 'Creative'
}

/** Still-image generation (M6): a prompt in, a stored asset and its ledgered
 * cost out. This route is the only path in the plugin that calls a provider
 * to create image bytes (`image_routes.py`'s module docstring), so every
 * asset it writes is, by construction, this campaign's source creative.
 *
 * **This panel shows the campaign's existing creative, not only what this
 * browser session generated** — issue #102, and the reason it is a defect
 * rather than a limitation is money. Generation is the one irreversibly
 * billable step in the plugin (`image_routes.py`'s ordering rationale for
 * issue #24). This panel used to hold results in component state alone and
 * say "No stills generated yet this session" otherwise, which an operator
 * reasonably read as "this campaign has no creative" — and the one action
 * that reading invites is the one that charges the provider again. It now
 * reads `GET /campaigns/{id}/assets` on mount.
 *
 * That is a **different route from `/pack`**, deliberately: `/pack` is gated
 * on approved copy (404/409 before then — precisely the campaign whose
 * operator most needs to see what exists) and mints tracked links as a side
 * effect, which nothing that loads on mount may do. See
 * `pack_routes.list_assets_route`.
 *
 * **A failed load must never read as "nothing exists".** The empty state is
 * shown only when the read actually came back empty; if it failed, this says
 * so and names the consequence, because "we could not check" and "there is
 * nothing here" differ by the price of one generation.
 */
export function ImageGenerator({ campaignId }: { campaignId: string }) {
  const [prompt, setPrompt] = useState('')
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [generated, setGenerated] = useState<GenerateStillResponse[]>([])
  const [existing, setExisting] = useState<PackAsset[] | null>(null)
  const [loadingExisting, setLoadingExisting] = useState(true)
  const [existingError, setExistingError] = useState<string | null>(null)

  const loadExisting = useCallback(async () => {
    setLoadingExisting(true)
    try {
      const response = await getCampaignAssets(campaignId)
      setExisting(response.assets)
      setExistingError(null)
    } catch (e: unknown) {
      // `existing` is deliberately NOT cleared here: a failed refresh must
      // not delete creative already on screen, which would recreate exactly
      // the "nothing exists" reading this whole change removes.
      setExistingError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoadingExisting(false)
    }
  }, [campaignId])

  useEffect(() => {
    void loadExisting()
  }, [loadExisting])

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setGenerating(true)
    setError(null)
    try {
      const result = await generateStill(campaignId, prompt.trim())
      setGenerated((prev) => [result, ...prev])
      setPrompt('')
      // Picks up the placement renders `generate_still_route` writes after
      // the source row (issue #36) — the response only carries the source.
      void loadExisting()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setGenerating(false)
    }
  }

  // A still generated in this session is also, after the refresh above, a row
  // in `existing`. Show it once — as the session entry, which is the one
  // carrying what it cost.
  const sessionAssetIds = new Set(
    generated.map((g) => g.asset?.id).filter((id): id is string => typeof id === 'string'),
  )
  const existingToShow = (existing ?? []).filter((a) => !sessionAssetIds.has(a.id))
  const nothingToShow = generated.length === 0 && existingToShow.length === 0

  return (
    <section className="images" aria-label="Still images">
      <form onSubmit={submit} aria-label="Generate a still image">
        <label htmlFor={`prompt-${campaignId}`}>Prompt</label>
        <textarea
          id={`prompt-${campaignId}`}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="A bright, welcoming photo of a small business storefront…"
        />
        <button type="submit" disabled={prompt.trim() === '' || generating}>
          {generating ? 'Generating…' : 'Generate still'}
        </button>
      </form>

      {error !== null && <p className="error">{error}</p>}

      {existingError !== null && (
        <p className="warning">
          Could not check this campaign for existing creative: {existingError} — generating now may
          pay for an image this campaign already has.
        </p>
      )}

      {nothingToShow && loadingExisting && <p className="empty">Loading existing creative…</p>}

      {nothingToShow && !loadingExisting && existingError === null && (
        <p className="empty">No creative for this campaign yet.</p>
      )}

      {!nothingToShow && (
        <ul className="stills">
          {generated.map((g) => (
            <li key={g.media.id}>
              <img src={g.url} alt="Generated still" />
              <p className="cost">Cost: {formatCost(g.ledger)}</p>
            </li>
          ))}
          {existingToShow.map((a) => (
            <li key={a.id}>
              <img src={a.url} alt={assetLabel(a)} />
              <span className="placement">{assetLabel(a)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

import { useState } from 'react'

import { generateStill, type GenerateStillResponse } from '../lib/api'

/** `cost_usd` is nullable and the null case is load-bearing (`ledger.py` /
 * `image_routes.py`): show "unpriced", never £0 — a £0 reads as "this cost
 * nothing", which is a different and false claim from "this deployment could
 * not price it". */
function formatCost(ledger: GenerateStillResponse['ledger']): string {
  if (ledger.unpriced || ledger.cost_usd === null) return 'unpriced'
  return `$${ledger.cost_usd.toFixed(4)}`
}

/** Still-image generation (M6): a prompt in, a stored asset and its ledgered
 * cost out. This route is the only path in the plugin that calls a provider
 * to create image bytes (`image_routes.py`'s module docstring), so every
 * asset it writes is, by construction, this campaign's source creative.
 *
 * Only shows stills generated in this browser session — there is no
 * admin-reachable route to resolve a stored asset back to a fresh URL outside
 * pack assembly (`pack_routes.py`'s `_asset_with_url` uses the SigV4-signed
 * internal client this surface cannot reach), so a history of past
 * generations belongs to the distribution pack view, not here.
 */
export function ImageGenerator({ campaignId }: { campaignId: string }) {
  const [prompt, setPrompt] = useState('')
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [generated, setGenerated] = useState<GenerateStillResponse[]>([])

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setGenerating(true)
    setError(null)
    try {
      const result = await generateStill(campaignId, prompt.trim())
      setGenerated((prev) => [result, ...prev])
      setPrompt('')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setGenerating(false)
    }
  }

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

      {generated.length === 0 && <p className="empty">No stills generated yet this session.</p>}

      {generated.length > 0 && (
        <ul className="stills">
          {generated.map((g) => (
            <li key={g.media.id}>
              <img src={g.url} alt="Generated still" />
              <p className="cost">Cost: {formatCost(g.ledger)}</p>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

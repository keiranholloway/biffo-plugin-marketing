import { useEffect, useState } from 'react'

import { CampaignDetail } from './components/CampaignDetail'
import { MintLinks } from './components/MintLinks'
import { createCampaign, listCampaigns, type Campaign } from './lib/api'
import { useChannelTaxonomy } from './lib/useChannelTaxonomy'

/** The campaign studio's admin surface.
 *
 * A list, and a form to add to it, and — once a campaign exists — the whole
 * studio behind it: the brief, the pipeline gates (M3–M5), image generation
 * (M6), both distribution packs (M5/M9), and results (M8). The first version
 * of this panel was list-only, and the only way to add anything was a
 * `fetch` pasted into devtools; this is the second gap of the same shape —
 * seven milestones of API with no way to reach them from a browser.
 */
export default function App() {
  const [campaigns, setCampaigns] = useState<Campaign[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<Campaign | null>(null)

  const [name, setName] = useState('')
  const [destination, setDestination] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  // Fetched once here, not once per row's `MintLinks` (#84) — the same
  // one-fetch-per-view sharing `CampaignDetail` already does for
  // `Pipeline`/`DistributionPack` via this same hook.
  const channelLookup = useChannelTaxonomy()

  function load() {
    listCampaigns()
      .then(setCampaigns)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
  }

  useEffect(load, [])

  function handleCampaignUpdated(updated: Campaign) {
    setSelected(updated)
    setCampaigns((prev) => prev?.map((c) => (c.id === updated.id ? updated : c)) ?? prev)
  }

  if (selected !== null) {
    return (
      <main>
        <CampaignDetail
          campaign={selected}
          onBack={() => setSelected(null)}
          onCampaignUpdated={handleCampaignUpdated}
        />
      </main>
    )
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setSaving(true)
    setSaveError(null)
    try {
      await createCampaign({ name: name.trim(), destination_url: destination.trim() })
      setName('')
      setDestination('')
      load()
    } catch (e: unknown) {
      setSaveError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const canSubmit = name.trim() !== '' && destination.trim() !== '' && !saving

  return (
    <main>
      <h1>Campaign studio</h1>
      <p className="lede">Campaigns for this tenant, and the tracked links minted from them.</p>

      <form onSubmit={submit} aria-label="Create a campaign">
        <h2>New campaign</h2>
        <label htmlFor="name">Name</label>
        <input
          id="name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Spring demo push"
        />

        <label htmlFor="destination">Destination URL</label>
        <input
          id="destination"
          value={destination}
          onChange={(e) => setDestination(e.target.value)}
          placeholder="https://dev.tabsii.com/intake/demo"
        />
        <p className="hint">
          Where tracked links send people. Every minted link carries this URL with the
          campaign&rsquo;s own id as <code>utm_campaign</code>.
        </p>

        <button type="submit" disabled={!canSubmit}>
          {saving ? 'Creating…' : 'Create campaign'}
        </button>

        {saveError !== null && <p className="error">{saveError}</p>}
      </form>

      {error !== null && <p className="empty">Could not load campaigns: {error}</p>}
      {error === null && campaigns === null && <p className="empty">Loading…</p>}

      {campaigns !== null && campaigns.length === 0 && (
        <p className="empty">No campaigns yet — create one above.</p>
      )}

      {campaigns !== null && campaigns.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Status</th>
              <th>Destination</th>
              <th>Tracked links</th>
              <th>Studio</th>
            </tr>
          </thead>
          <tbody>
            {campaigns.map((c) => (
              <tr key={c.id}>
                <td>{c.name}</td>
                <td>
                  <span className={`badge badge-${c.status}`}>{c.status}</span>
                </td>
                <td>{c.destination_url ?? '—'}</td>
                <td>
                  {/* Collapsed behind a disclosure so a row with tracked
                      links to mint is still one line — the full channel
                      picker used to render inline in every row, which is a
                      per-row ACTION, not table content. */}
                  <details className="mint-toggle">
                    <summary>Mint link</summary>
                    <MintLinks campaignId={c.id} channelLookup={channelLookup} />
                  </details>
                </td>
                <td>
                  <button type="button" onClick={() => setSelected(c)}>
                    Open
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  )
}

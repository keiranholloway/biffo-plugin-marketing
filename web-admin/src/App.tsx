import { useEffect, useState } from 'react'

import { listCampaigns, type Campaign } from './lib/api'

/** The campaign studio's admin surface.
 *
 * Deliberately a list and nothing else for now. It exists because the shared
 * plugin host will not package a plugin declaring `admin_ingress` without a
 * built `web-admin/dist` — a shell that shows real data is the smallest honest
 * thing that satisfies that, and it is what makes M2 demonstrable.
 */
export default function App() {
  const [campaigns, setCampaigns] = useState<Campaign[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    listCampaigns()
      .then(setCampaigns)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
  }, [])

  return (
    <main>
      <h1>Campaign studio</h1>
      <p className="lede">Campaigns for this tenant, and the tracked links minted from them.</p>

      {error !== null && <p className="empty">Could not load campaigns: {error}</p>}

      {error === null && campaigns === null && <p className="empty">Loading…</p>}

      {campaigns !== null && campaigns.length === 0 && (
        <p className="empty">
          No campaigns yet. Create one with <code>POST /api/v1/plugins/marketing/campaigns</code>.
        </p>
      )}

      {campaigns !== null && campaigns.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Status</th>
              <th>Destination</th>
            </tr>
          </thead>
          <tbody>
            {campaigns.map((c) => (
              <tr key={c.id}>
                <td>{c.name}</td>
                <td>{c.status}</td>
                <td>{c.destination_url ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  )
}

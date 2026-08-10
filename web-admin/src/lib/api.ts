/** Calls to the plugin's own generated CRUD, served by Core.
 *
 * Same-origin and relative: this app is served from
 * `/api/v1/plugins/marketing/admin/`, so the API sits one level up at
 * `/api/v1/plugins/marketing/`. Hard-coding an absolute origin here would
 * break the moment the instance's domain differs, which is every instance
 * other than the one it was written on.
 */

export interface Campaign {
  id: string
  name: string
  status: string
  destination_url: string | null
}

const BASE = '/api/v1/plugins/marketing'

export async function listCampaigns(): Promise<Campaign[]> {
  const response = await fetch(`${BASE}/campaigns`, { credentials: 'include' })

  if (!response.ok) {
    // The status, not the body. A body can be an HTML error page — rendering
    // it into the message is how another plugin ended up showing
    // `{"detail":"Administrator access required"}` where its content belonged.
    throw new Error(`request failed (${response.status})`)
  }

  const body: unknown = await response.json()
  return Array.isArray(body) ? (body as Campaign[]) : []
}

/** Calls to the plugin's own generated CRUD, served by Core.
 *
 * Same-origin and relative: this app is served from
 * `/api/v1/plugins/marketing/admin/`, so the API sits one level up at
 * `/api/v1/plugins/marketing/`. Hard-coding an absolute origin would break on
 * every instance but the one it was written on.
 *
 * ## Why the Authorization header, and not cookies
 *
 * The first version of this file used `credentials: 'include'`, which sends
 * cookies. API Gateway's `/api/v1/plugins/*` route is JWT-authorized, so a
 * cookie is not a credential it recognises: every call came back 401 and the
 * panel rendered "Could not load campaigns: request failed (401)".
 *
 * The estate authenticates with a Cognito ID token in `Authorization: Bearer`,
 * resolved from the portal's shared session — `auth.ts`/`identity.ts` here are
 * idea-scout's, copied rather than rewritten, because this is exactly the part
 * that should not be reinvented per plugin.
 */

import { getCurrentSession } from './auth'

export interface Campaign {
  id: string
  name: string
  status: string
  destination_url: string | null
}

const BASE = '/api/v1/plugins/marketing'

/** What a person supplies to create a campaign. Everything else Core derives. */
export interface NewCampaign {
  name: string
  destination_url: string
}

/** Create a campaign.
 *
 * Requires the `admin` role — `marketing_campaign`'s own `create` permission in
 * the manifest, evaluated by Core, not by this app. A caller without it gets a
 * 403 and is told so, rather than the form appearing to work.
 */
export async function createCampaign(input: NewCampaign): Promise<Campaign> {
  const session = await getCurrentSession()
  const idToken = session?.getIdToken().getJwtToken() ?? null

  const response = await fetch(`${BASE}/campaigns`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(idToken ? { Authorization: `Bearer ${idToken}` } : {}),
    },
    // `status` is the pipeline's own vocabulary (definitions.PIPELINE_STAGES);
    // a new campaign always starts at draft, so the form does not offer it.
    body: JSON.stringify({ ...input, status: 'draft' }),
  })

  if (!response.ok) {
    if (response.status === 401) {
      throw new Error('not signed in (401) — sign in to the portal, then reload')
    }
    if (response.status === 403) {
      throw new Error('you need the admin role to create a campaign (403)')
    }
    throw new Error(`could not create the campaign (${response.status})`)
  }

  return (await response.json()) as Campaign
}

export async function listCampaigns(): Promise<Campaign[]> {
  const session = await getCurrentSession()
  const idToken = session?.getIdToken().getJwtToken() ?? null

  const response = await fetch(`${BASE}/campaigns`, {
    headers: {
      ...(idToken ? { Authorization: `Bearer ${idToken}` } : {}),
    },
  })

  if (!response.ok) {
    // The status, not the body. A body can be an HTML error page — rendering it
    // is how another plugin displayed `{"detail":"Administrator access
    // required"}` where its content belonged.
    if (response.status === 401) {
      throw new Error('not signed in (401) — sign in to the portal, then reload')
    }
    throw new Error(`request failed (${response.status})`)
  }

  const body: unknown = await response.json()
  return Array.isArray(body) ? (body as Campaign[]) : []
}

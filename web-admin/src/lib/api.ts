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

//: Minting is NOT generated CRUD — it reads one row and writes several, and the
//: values it writes are derived (the token, and the destination with its UTMs).
//: So it lives on the plugin's own admin app, one path segment deeper.
const ADMIN_BASE = '/api/v1/plugins/marketing/admin'

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


/** One tracked link, as minted. */
export interface MintedLink {
  id: string
  channel: string
  variant: string | null
  is_paid: boolean
  /** The URL to publish. `c/<token>` on this instance's own origin. */
  url: string
}

/** Mint tracked links for a campaign.
 *
 * The caller supplies only a channel (and optionally a variant, and whether it
 * is paid). The token and the destination — including `utm_campaign`, which is
 * the campaign's own id — are derived server-side and cannot be supplied. That
 * is the point of the whole milestone, so the form does not offer them.
 */
export async function mintLinks(
  campaignId: string,
  links: { channel: string; variant?: string; is_paid?: boolean }[],
): Promise<MintedLink[]> {
  const session = await getCurrentSession()
  const idToken = session?.getIdToken().getJwtToken() ?? null

  const response = await fetch(`${ADMIN_BASE}/campaigns/${campaignId}/links`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(idToken ? { Authorization: `Bearer ${idToken}` } : {}),
    },
    body: JSON.stringify({ links }),
  })

  if (!response.ok) {
    if (response.status === 401) {
      throw new Error('not signed in (401) — sign in to the portal, then reload')
    }
    if (response.status === 403) {
      throw new Error('you need the admin role to mint links (403)')
    }
    if (response.status === 422) {
      throw new Error('this campaign has no destination URL, so its links would lead nowhere')
    }
    if (response.status === 503) {
      throw new Error('this deployment has no public base URL configured, so links cannot be minted')
    }
    throw new Error(`could not mint links (${response.status})`)
  }

  const body = (await response.json()) as { links?: MintedLink[] }
  return body.links ?? []
}

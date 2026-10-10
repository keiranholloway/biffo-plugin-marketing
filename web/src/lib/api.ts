/** Calls to the plugin's founder-facing JSON app (`user_app.py`).
 *
 * Same-origin and relative: the host serves this UI at
 * `/api/v1/plugins/marketing/ui/` and the API one level up at
 * `/api/v1/plugins/marketing/`. The bearer token is resolved fresh per request
 * from the portal's shared session (see `auth.ts`), never captured at mount.
 */
import { createRequest, ApiError } from './api-core'
import { getFreshIdToken } from './auth'

export const API_BASE = '/api/v1/plugins/marketing'

export const MEDIA_KINDS = ['copy', 'image', 'video', 'audio'] as const
export type MediaKind = (typeof MEDIA_KINDS)[number]
export const MOTIONS = ['organic', 'paid', 'both'] as const
export type Motion = (typeof MOTIONS)[number]

export interface CampaignSummary {
  id: string
  name: string
  status: string
  starts_at?: string | null
  ends_at?: string | null
}

export interface NewCampaign {
  name: string
  brief?: string
  destination_url?: string
  media_kinds?: MediaKind[]
  motion?: Motion
}

export interface CreatedCampaign {
  id: string | null
  name: string
  status: string
}

export function createApi(getIdToken = getFreshIdToken) {
  const request = createRequest(getIdToken, API_BASE, async (res, context) => {
    let detail = ''
    try {
      const data = (await res.json()) as { detail?: unknown }
      if (typeof data.detail === 'string') detail = data.detail
      else if (Array.isArray(data.detail)) {
        detail = data.detail
          .map((d) => (typeof d === 'object' && d && 'msg' in d ? String((d as { msg: unknown }).msg) : ''))
          .filter(Boolean)
          .join('; ')
      }
    } catch {
      // body was not JSON — fall back to the status line below
    }
    if (res.status === 401 || res.status === 403) {
      throw new ApiError(res.status, `You do not have access to ${context ?? 'this'}.`)
    }
    throw new ApiError(res.status, detail || `${context ?? 'Request'} failed (${res.status})`)
  })

  return {
    createCampaign: (body: NewCampaign) =>
      request<CreatedCampaign>('POST', '/campaigns', body, undefined, 'creating a campaign'),
    listCampaigns: () =>
      request<CampaignSummary[]>('GET', '/campaigns', undefined, undefined, 'campaigns'),
  }
}

export type Api = ReturnType<typeof createApi>

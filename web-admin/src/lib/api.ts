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
 *
 * ## Why this file does NOT delegate its fetching to `./api-core`
 *
 * `createRequest` (biffo-template#1492) is deliberately shared plumbing: fetch
 * + bearer header + a generic `ApiError(status, text)` built from
 * `res.text()`. This plugin's endpoints do something `createRequest` does not
 * — they read the JSON `detail` field the plugin's own admin routes author
 * specifically for an operator (`throwForResponse`/`detailMessage` below), and
 * they reword 401/403/422/503 per endpoint ("you need the admin role to mint
 * links", "this campaign has no destination URL"). `createRequest` throws
 * before handing back the `Response`, so there is no seam left to layer that
 * per-endpoint wording on top of it — adopting it here would collapse every
 * one of those messages into one generic status-coded string, which is a
 * behaviour change, not a refactor. That mismatch belongs upstream as a
 * question for `api-core` (e.g. a hook to inspect the body before throwing),
 * not silently absorbed here.
 *
 * `getFreshIdToken()` (auth.ts) was tried as a drop-in for the
 * `getCurrentSession()` + `.getIdToken().getJwtToken()` pair every request
 * function here repeats inline, and rejected for the same reason: not because
 * it behaves differently (it doesn't — it is that exact pair, under one name,
 * and this file already re-resolved a fresh token per call before this
 * migration, by this same route), but because `api.test.ts` stubs
 * `auth.getCurrentSession` via `vi.spyOn`, and `getFreshIdToken`'s internal
 * call to `getCurrentSession` is a same-module reference Vitest's ESM spy does
 * not intercept — three tests then hit the real (unmocked) Cognito pool
 * resolution instead of the stub. Fixing that means changing what the test
 * stubs, which is outside this migration's remit ("behaviour must not change,
 * tests pass unchanged"). Kept as `getCurrentSession()` inline, exactly as
 * before, in all four call sites (`createCampaign`, `listCampaigns`,
 * `mintLinks`, `authedFetch`) — still per-request, still fresh every time.
 */

import { getCurrentSession } from './auth'

export interface Campaign {
  id: string
  name: string
  status: string
  destination_url: string | null
  brief?: string | null
  guidance?: string | null
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

// ── The rest of the campaign studio (M3–M6, M8, M9) ──────────────────────────
//
// Everything below calls `${ADMIN_BASE}` — the plugin's own routes
// (`admin_app.py`, `channel_plan_routes.py`, `copy_routes.py`,
// `image_routes.py`, `pack_routes.py`, `paid_pack_routes.py`,
// `results_routes.py`), never generated CRUD directly, for the same reason
// `mintLinks` above does not: each of these reads one row and writes several,
// or assembles several tables into one response.
//
// `throwForResponse` differs from `listCampaigns`/`createCampaign`/
// `mintLinks` above on purpose: those hit Core's *generic* CRUD, whose 403
// text ("Administrator access required") is Core's own wording, not this
// plugin's, and the test above deliberately does not surface it verbatim.
// Everything below hits THIS plugin's own admin routes, whose `detail` text
// is hand-authored specifically to be read by the operator — "This surface
// requires the 'admin' group" (`require_group`'s own message), "This
// campaign has no destination_url, so its links would lead nowhere", "The
// research artefact must be approved before this can proceed (status:
// proposed)". Extracting and showing that string is the point, not a
// regression of the "never render a raw response body" rule: only a `detail`
// field that is actually a string is ever shown, so an HTML error page or an
// unparseable body still falls back to a plain status-coded message rather
// than being dumped on screen.

async function authedFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const session = await getCurrentSession()
  const idToken = session?.getIdToken().getJwtToken() ?? null
  return fetch(path, {
    ...init,
    headers: {
      ...(init.body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...(idToken ? { Authorization: `Bearer ${idToken}` } : {}),
      ...(init.headers ?? {}),
    },
  })
}

async function detailMessage(response: Response): Promise<string | null> {
  try {
    const body: unknown = await response.json()
    if (
      body !== null &&
      typeof body === 'object' &&
      'detail' in body &&
      typeof (body as { detail?: unknown }).detail === 'string'
    ) {
      return (body as { detail: string }).detail
    }
  } catch {
    // Not JSON, or an empty body — nothing safe to extract, fall through to a
    // generic message rather than guessing at unstructured text.
  }
  return null
}

async function throwForResponse(response: Response, context: string): Promise<never> {
  if (response.status === 401) {
    throw new Error('not signed in (401) — sign in to the portal, then reload')
  }
  const detail = await detailMessage(response)
  if (response.status === 403) {
    throw new Error(detail !== null ? `${detail} (403)` : `you need the admin role to ${context} (403)`)
  }
  if (detail !== null) {
    throw new Error(`${detail} (${response.status})`)
  }
  throw new Error(`could not ${context} (${response.status})`)
}

/** Update a campaign's own fields directly — generated CRUD, not an admin-app
 * route. Used here for `brief`: nothing in the create form collects one (see
 * `App.tsx`), and without a brief `start_research_route` 422s, so the
 * pipeline's first stage can never start. */
export async function updateCampaign(
  campaignId: string,
  patch: Partial<Pick<Campaign, 'name' | 'destination_url' | 'brief' | 'guidance'>>,
): Promise<Campaign> {
  const response = await authedFetch(`${BASE}/campaigns/${campaignId}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  })
  if (!response.ok) return throwForResponse(response, 'update the campaign')
  return (await response.json()) as Campaign
}

// ── The pipeline (M3/M4/M5): research → positioning → channel plan → copy ───

export interface Source {
  url: string
  note: string
}

export interface ResearchFinding {
  signal: string
  why_it_matters: string
  sources: Source[]
}

export interface ResearchSynthesisBody {
  summary: string
  findings: ResearchFinding[]
}

export interface Segment {
  name: string
  description: string
  sources: Source[]
}

export interface MessagePillar {
  pillar: string
  rationale: string
  sources: Source[]
}

export interface CallToAction {
  text: string
  rationale: string
  sources: Source[]
}

export interface PositioningBody {
  segments: Segment[]
  pillars: MessagePillar[]
  ctas: CallToAction[]
}

export interface ChannelRecommendation {
  channel: string
  motion: 'organic' | 'paid'
  rank: number
  rationale: string
  sources: Source[]
}

export interface ChannelPlanBody {
  channels: ChannelRecommendation[]
}

export interface ChannelCopy {
  channel: string
  motion: 'organic' | 'paid'
  headline: string
  body: string
  cta: string
  sources: Source[]
}

export interface CopySetBody {
  channels: ChannelCopy[]
}

export type ArtefactKind = 'research' | 'positioning' | 'channel_plan' | 'copy'
export type ArtefactStatus = 'pending' | 'proposed' | 'approved' | 'rejected'

/** One pipeline-stage artefact row. `body`/`citations` are JSON-serialised
 * text columns (plugin tables have no JSON column type — `biffo.plugin.json`'s
 * own description of `marketing_artefact.body`), not parsed objects — use
 * {@link parseArtefactBody}. `body` is only the real stage output once
 * `status` has left `pending`; while `pending` it may hold bookkeeping (e.g.
 * research's in-flight run ids) that no renderer here should try to read. */
export interface Artefact {
  id: string
  campaign_id: string
  kind: ArtefactKind
  status: ArtefactStatus
  body: string | null
  citations: string | null
  causation_id: string | null
  agent_run_id: string | null
}

/** Parse an artefact's `body` against its kind's known shape, or `null` if
 * there is nothing to parse (no artefact, no body, or — defensively — a body
 * that does not parse as JSON). */
export function parseArtefactBody<T>(artefact: Artefact | null): T | null {
  if (artefact === null || artefact.body === null || artefact.body === '') return null
  try {
    return JSON.parse(artefact.body) as T
  } catch {
    return null
  }
}

/** This campaign's latest artefact of `kind`, advancing it first if it is
 * still `pending`. `null` on a 404 — "no artefact of this kind yet" is a
 * normal state (nothing has been started), not an error to show. */
export async function getArtefact(campaignId: string, kind: ArtefactKind): Promise<Artefact | null> {
  const response = await authedFetch(`${ADMIN_BASE}/campaigns/${campaignId}/artefacts/${kind}`)
  if (response.status === 404) return null
  if (!response.ok) return throwForResponse(response, `load the ${kind.replace('_', ' ')} stage`)
  return (await response.json()) as Artefact
}

export async function approveArtefact(campaignId: string, kind: ArtefactKind): Promise<Artefact> {
  const response = await authedFetch(
    `${ADMIN_BASE}/campaigns/${campaignId}/artefacts/${kind}/approve`,
    { method: 'POST' },
  )
  if (!response.ok) return throwForResponse(response, `approve the ${kind.replace('_', ' ')} stage`)
  return (await response.json()) as Artefact
}

export async function rejectArtefact(campaignId: string, kind: ArtefactKind): Promise<Artefact> {
  const response = await authedFetch(
    `${ADMIN_BASE}/campaigns/${campaignId}/artefacts/${kind}/reject`,
    { method: 'POST' },
  )
  if (!response.ok) return throwForResponse(response, `reject the ${kind.replace('_', ' ')} stage`)
  return (await response.json()) as Artefact
}

export async function startResearch(campaignId: string): Promise<Artefact> {
  const response = await authedFetch(`${ADMIN_BASE}/campaigns/${campaignId}/research`, {
    method: 'POST',
  })
  if (!response.ok) return throwForResponse(response, 'start research')
  return (await response.json()) as Artefact
}

export async function startPositioning(campaignId: string): Promise<Artefact> {
  const response = await authedFetch(`${ADMIN_BASE}/campaigns/${campaignId}/positioning`, {
    method: 'POST',
  })
  if (!response.ok) return throwForResponse(response, 'start positioning')
  return (await response.json()) as Artefact
}

export async function startChannelPlan(campaignId: string): Promise<Artefact> {
  const response = await authedFetch(`${ADMIN_BASE}/campaigns/${campaignId}/channel-plan`, {
    method: 'POST',
  })
  if (!response.ok) return throwForResponse(response, 'start channel planning')
  return (await response.json()) as Artefact
}

export async function startCopy(campaignId: string): Promise<Artefact> {
  const response = await authedFetch(`${ADMIN_BASE}/campaigns/${campaignId}/copy`, {
    method: 'POST',
  })
  if (!response.ok) return throwForResponse(response, 'start copy generation')
  return (await response.json()) as Artefact
}

// ── Images (M6) ───────────────────────────────────────────────────────────────

export interface GenerateStillResponse {
  asset: Record<string, unknown>
  media: { id: string } & Record<string, unknown>
  url: string
  /** `cost_usd` is nullable and the null case is load-bearing — `unpriced`
   * says so explicitly so a caller never has to treat a missing cost as
   * £0. */
  ledger: { id: string; cost_usd: number | null; unpriced: boolean }
}

export async function generateStill(campaignId: string, prompt: string): Promise<GenerateStillResponse> {
  const response = await authedFetch(`${ADMIN_BASE}/campaigns/${campaignId}/stills`, {
    method: 'POST',
    body: JSON.stringify({ prompt }),
  })
  if (!response.ok) return throwForResponse(response, 'generate an image')
  return (await response.json()) as GenerateStillResponse
}

// ── The distribution pack (M5) and paid pack (M9) ────────────────────────────

export interface PackAsset {
  id: string
  campaign_id: string
  media_kind: string
  placement: string | null
  media_id: string
  is_source: boolean | null
  url: string
}

export interface PackLink {
  channel: string
  variant: string | null
  is_paid: boolean | null
  /** `null` when this deployment has no public base URL configured — the
   * pack still lists the channel, just with nothing to publish yet. */
  url: string | null
}

export interface Pack {
  campaign_id: string
  assets: PackAsset[]
  /** Placements `PLACEMENTS` names that have no rendered asset yet — reported
   * honestly rather than the pack silently omitting them (issue #36 is the
   * tracked gap that causes this). */
  missing_placements: string[]
  copy: ChannelCopy[]
  links: PackLink[]
  guidance: string
}

export async function getPack(campaignId: string): Promise<Pack> {
  const response = await authedFetch(`${ADMIN_BASE}/campaigns/${campaignId}/pack`)
  if (!response.ok) return throwForResponse(response, 'load the distribution pack')
  return (await response.json()) as Pack
}

export interface AdCopyVariant {
  channel: string
  platform: string
  headline: string
  headline_limit: number
  headline_truncated: boolean
  body: string
  body_limit: number
  body_truncated: boolean
  cta: string
  cta_limit: number
  cta_truncated: boolean
}

export interface BudgetRecommendation {
  currency: string
  channel_count: number
  per_channel_daily: number
  total_daily: number
  test_window_days: number
  total_test_budget: number
  basis: string
}

export interface PaidPack {
  campaign_id: string
  ad_copy: AdCopyVariant[]
  assets: PackAsset[]
  missing_placements: string[]
  targeting: Segment[]
  budget: BudgetRecommendation
  links: PackLink[]
  guidance: string
  /** This plugin has no reachable transport to spend data (issue #31) — see
   * {@link UnmeasuredMetric}. */
  spend: UnmeasuredMetric
}

export async function getPaidPack(campaignId: string): Promise<PaidPack> {
  const response = await authedFetch(`${ADMIN_BASE}/campaigns/${campaignId}/paid-pack`)
  if (!response.ok) return throwForResponse(response, 'load the paid pack')
  return (await response.json()) as PaidPack
}

// ── Results (M8) ──────────────────────────────────────────────────────────────

export interface ClickBreakdown {
  total: number
  paid: number
  organic: number
  unknown_channel_type: number
}

/** A metric this endpoint cannot compute at all today — never a bare `null`
 * or a `0`. `measurable` is always `false` on every instance the API
 * constructs; `reason` says why. `denominator` is the population this metric
 * would be a share of once it becomes measurable, when that population is
 * itself known; `null` when it isn't. Render "not measurable" distinctly from
 * zero — this plugin has no reachable transport to `demo_requests`/
 * `lead_source_costs` (issue #31), so reporting zero would claim "we looked,
 * and nothing happened," which is not a claim it can make today. */
export interface UnmeasuredMetric {
  measurable: false
  denominator: number | null
  reason: string
}

export interface CampaignResults {
  campaign_id: string
  campaign_name: string
  clicks: ClickBreakdown
  leads: UnmeasuredMetric
  conversions: UnmeasuredMetric
  cost: UnmeasuredMetric
}

export interface ResultsResponse {
  campaigns: CampaignResults[]
  /** Clicks whose `campaign_id` named no campaign this call could see —
   * counted rather than silently dropped from every campaign's total. */
  unattributed_clicks: number
}

export async function getResults(): Promise<ResultsResponse> {
  const response = await authedFetch(`${ADMIN_BASE}/results`)
  if (!response.ok) return throwForResponse(response, 'load results')
  return (await response.json()) as ResultsResponse
}

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
import { createRequest } from './api-core'

/** Thrown by {@link approveArtefact} specifically for `approve_artefact_route`'s
 * "Unknown element id(s)" 422 (issue #145, `admin_app.py`) — the artefact
 * changed under the operator (someone re-ran the stage, or the page has been
 * open since before this run) so the ids it is holding are no longer the
 * artefact's own. Distinguished from every other failure this route can
 * return (403, "not proposed" 409, an empty-selection 422) because it is the
 * one case where the right response is not "read the message" but "reload
 * and choose again" — a stale id cannot be retried into success. */
export class StaleArtefactError extends Error {}

/** Thrown by {@link getArtefact} for the advance route's 502 (issue #159) —
 * "an agent ran, and what it produced (or failed to produce) is not something
 * retrying the *request* fixes; the operator re-runs the stage instead". That
 * sentence is `admin_app._pipeline_error_to_http`'s own docstring, and 502 is
 * the only status this route uses to say it: every `pipeline.PipelineError`
 * maps here and nothing else does.
 *
 * Distinguished from every other failure a stage read can produce because it
 * is the one that "Check for result" can never resolve. The artefact is still
 * `pending` — nothing ever moved it on — so re-reading it re-runs the same
 * extraction against the same dead run and returns the same 502 for as long
 * as anyone keeps pressing. On tabsii dev, 2026-08-14, that left a campaign
 * sitting at `Running…` with `the channel-plan run produced no
 * submit_channel_plan tool call` and no route forward at all.
 *
 * A 401/403 (the operator's session) or a 5xx from Core itself is NOT this:
 * re-running would bill for a fresh agent run to fix something no agent
 * caused. Hence the narrow status match rather than "any failure while
 * pending".
 *
 * Carries the same message the plain `Error` did, so nothing that only
 * renders `error` changes wording — see `throwForResponse`. */
export class StageRunFailedError extends Error {}

/** How a campaign reaches people (#67). Wider than a channel's own motion:
 * a channel is organic OR paid, a campaign may run `both`. */
export type CampaignMotion = 'organic' | 'paid' | 'both'

export interface Campaign {
  id: string
  name: string
  status: string
  destination_url: string | null
  brief?: string | null
  guidance?: string | null
  /** #67. `null`/absent on every campaign created before targeting existed —
   * the channel-plan stage refuses to start until it is set, rather than
   * defaulting to `both` and deciding for the operator. */
  motion?: CampaignMotion | null
  /** Comma-separated `marketing_channel.key` values — the channels this
   * campaign may plan against (#67). Stored as one string on the campaign,
   * the same shape `media_kinds` uses; parse with
   * `lib/campaignTargeting`'s `targetChannelKeys`. */
  target_channel_keys?: string | null
}

const BASE = '/api/v1/plugins/marketing'

//: Minting is NOT generated CRUD — it reads one row and writes several, and the
//: values it writes are derived (the token, and the destination with its UTMs).
//: So it lives on the plugin's own admin app, one path segment deeper.
const ADMIN_BASE = '/api/v1/plugins/marketing/admin'

/** Resolved fresh on every call — never snapshotted. See `createRequest`'s own
 * doc in `api-core.ts` for why: a client built from a token captured once at
 * mount sends whatever was left on that token's remaining lifetime, and once
 * it lapses every call 401s for the rest of the page's life (ideation#69).
 *
 * This is NOT `auth.ts`'s own `getFreshIdToken()` — that helper calls
 * `getCurrentSession()` from *within* `auth.ts`, a same-module call that
 * `vi.spyOn(auth, 'getCurrentSession')` cannot intercept (the spy replaces the
 * export binding on the module namespace object, which a same-module callsite
 * never goes through). Every test here stubs `getCurrentSession` that way, so
 * adopting `getFreshIdToken` would silently stop being testable rather than
 * fail loudly. Calling `getCurrentSession` from this module instead is a
 * cross-module call the spy does intercept, and is behaviourally identical to
 * `getFreshIdToken`'s own body.
 */
async function getIdToken(): Promise<string | null> {
  const session = await getCurrentSession()
  return session?.getIdToken().getJwtToken() ?? null
}

/** One entry of a FastAPI/pydantic validation-error list — the shape `detail`
 * takes on a 422 raised by request-body validation itself (a `Field`
 * constraint), rather than by a plugin's own `raise HTTPException(...)`. See
 * {@link detailMessage}'s docstring for why this is a second shape `detail`
 * takes, not a malformed instance of the string one. */
interface ValidationErrorItem {
  loc?: unknown[]
  msg?: string
}

/** Turns a FastAPI validation-error list into one operator-readable line per
 * error — the field name (the last, most specific segment of `loc`, e.g.
 * `["body","links",0,"channel"]` → `channel`) and pydantic's own `msg`
 * ("String should have at most 64 characters"), which between them are
 * exactly "the field and the constraint" #83 asks the UI to show. `null`
 * only if every item is unusable (missing `msg`), so the caller still has a
 * status-coded fallback rather than an empty string. */
function formatValidationErrors(items: unknown[]): string | null {
  const lines = items
    .map((item): string | null => {
      if (item === null || typeof item !== 'object') return null
      const { loc, msg } = item as ValidationErrorItem
      if (typeof msg !== 'string') return null
      const field = Array.isArray(loc) && loc.length > 0 ? String(loc[loc.length - 1]) : null
      return field !== null ? `${field}: ${msg}` : msg
    })
    .filter((line): line is string => line !== null)
  return lines.length > 0 ? lines.join('; ') : null
}

/** Extracts an operator-readable reason from a failed response's body, or
 * `null` if there is nothing safe to show.
 *
 * `detail` takes TWO shapes here, both genuine, and conflating them is #83:
 * a **string** for every `HTTPException` this plugin raises itself (hand-
 * authored specifically to be read), and a **list** of
 * {@link ValidationErrorItem} for a 422 FastAPI raises on its own, before a
 * route body ever runs, when a `Field` constraint rejects the request (e.g.
 * `channel: str = Field(max_length=64)` on a channel over that length). The
 * first version of this function handled only the string case, so a
 * validation 422 fell through silently — not a wrong message, no message,
 * because nothing here recognised the shape at all.
 */
async function detailMessage(response: Response): Promise<string | null> {
  try {
    const body: unknown = await response.json()
    if (body === null || typeof body !== 'object' || !('detail' in body)) return null
    const detail = (body as { detail?: unknown }).detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) return formatValidationErrors(detail)
  } catch {
    // Not JSON, or an empty body — nothing safe to extract, fall through to a
    // generic message rather than guessing at unstructured text.
  }
  return null
}

/**
 * The `onError` mapper for `createRequest` (biffo-template#1492). Every
 * endpoint's error wording is reproduced here byte-for-byte, dispatched on the
 * per-call `context` string — nothing here is new behaviour, it is the same
 * hand-written branches that used to live inline in each function, moved
 * behind the hook `api-core.ts` added specifically so they would not have to
 * collapse into a generic status message.
 *
 * `createCampaign`/`listCampaigns`/`mintLinks` hit Core's *generic* CRUD (or,
 * for `mintLinks`, the host's own `group_gate` ahead of it), whose 403 text
 * ("Administrator access required") is Core's own wording, not this plugin's
 * — the tests deliberately do not surface it verbatim, so those three keep
 * their own hand-authored 403 message rather than reading `detail`.
 * `mintLinks`'s 422, unlike its 403, is NOT generic: it is either this
 * plugin's own hand-authored `HTTPException` (`admin_app.mint_links`'s "no
 * destination_url" and channel-taxonomy checks) or FastAPI's own validation
 * error for a `Field` constraint on the request body (both plugin-specific —
 * `MintRequest` is this plugin's own model), so it reads `detail` the same
 * way the `default` branch below does (#83 — the earlier hardcoded 422
 * message discarded both). Everything else hits THIS plugin's own admin
 * routes, whose `detail` text is hand-authored specifically to be read by the
 * operator — extracting and showing it is the point (see the file-level
 * comment below for the fuller version of this split).
 */
async function onError(response: Response, context: string | undefined): Promise<never> {
  if (response.status === 401) {
    throw new Error('not signed in (401) — sign in to the portal, then reload')
  }
  switch (context) {
    case undefined:
      // listCampaigns: the status, not the body. A body can be an HTML error
      // page — rendering it is how another plugin displayed
      // `{"detail":"Administrator access required"}` where its content
      // belonged.
      throw new Error(`request failed (${response.status})`)
    case 'create a campaign':
      if (response.status === 403) {
        throw new Error('you need the admin role to create a campaign (403)')
      }
      throw new Error(`could not create the campaign (${response.status})`)
    case 'mint links':
      if (response.status === 403) {
        throw new Error('you need the admin role to mint links (403)')
      }
      if (response.status === 422) {
        // Reads `detail` rather than assuming what a 422 here means (#83):
        // it is one of "no destination_url" (a hand-authored string), an
        // unrecognised channel key (#84, also a string), or a `Field`
        // constraint like `channel`'s 64-character ceiling (a validation
        // list) — three different causes that used to render the same
        // hardcoded sentence regardless of which one actually happened.
        // Falls back to the pre-#83 wording only when nothing readable came
        // back at all, so a genuinely bodyless 422 still says something.
        const detail = await detailMessage(response)
        if (detail !== null) {
          throw new Error(`${detail} (422)`)
        }
        throw new Error('this campaign has no destination URL, so its links would lead nowhere')
      }
      if (response.status === 503) {
        throw new Error('this deployment has no public base URL configured, so links cannot be minted')
      }
      throw new Error(`could not mint links (${response.status})`)
    default: {
      const detail = await detailMessage(response)
      if (response.status === 403) {
        throw new Error(detail !== null ? `${detail} (403)` : `you need the admin role to ${context} (403)`)
      }
      // `approve_artefact_route`'s "Unknown element id(s)" 422 (issue #145):
      // the artefact changed under the operator, so the selection they are
      // holding cannot succeed no matter how many times it is retried — the
      // one 422 this route returns that means "reload", not "read this and
      // adjust". Matched on the context (only an approve call reaches this
      // shape) and the detail text itself, since a "not proposed" 409 or the
      // empty-selection 422 from the very same route are NOT this case and
      // must keep their own plain-error rendering.
      if (
        response.status === 422 &&
        context !== undefined &&
        context.startsWith('approve the ') &&
        detail !== null &&
        detail.startsWith('Unknown element id')
      ) {
        throw new StaleArtefactError(detail)
      }
      if (detail !== null) {
        throw new Error(`${detail} (${response.status})`)
      }
      throw new Error(`could not ${context} (${response.status})`)
    }
  }
}

/** Bound to a fresh-per-call token source and the plugin's own default base.
 * `request(method, path, body?, base?, context?)` — see `api-core.ts`. */
const request = createRequest(getIdToken, BASE, onError)

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
  // `status` is the pipeline's own vocabulary (definitions.PIPELINE_STAGES); a
  // new campaign always starts at draft, so the form does not offer it.
  return request<Campaign>('POST', '/campaigns', { ...input, status: 'draft' }, BASE, 'create a campaign')
}

export async function listCampaigns(): Promise<Campaign[]> {
  const body = await request<unknown>('GET', '/campaigns')
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
 * The caller supplies a channel **key** from the taxonomy (`listChannels`),
 * and optionally a variant. The token and the destination — including
 * `utm_campaign`, which is the campaign's own id — are derived server-side
 * and cannot be supplied. That is the point of the whole milestone, so the
 * form does not offer them.
 *
 * No `is_paid` parameter (#84): motion lives on the channel now, so the
 * server derives `is_paid` from the selected channel's own taxonomy row
 * rather than trusting a second, independently-settable flag that could
 * disagree with it.
 */
export async function mintLinks(
  campaignId: string,
  links: { channel: string; variant?: string }[],
): Promise<MintedLink[]> {
  const body = await request<{ links?: MintedLink[] }>(
    'POST',
    `/campaigns/${campaignId}/links`,
    { links },
    ADMIN_BASE,
    'mint links',
  )
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
// `onError` above (`api-core.ts`'s hook) treats these differently on purpose:
// `listCampaigns`/`createCampaign`/`mintLinks` hit Core's *generic* CRUD,
// whose 403 text ("Administrator access required") is Core's own wording, not
// this plugin's, and the tests deliberately do not surface it verbatim.
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

/** `getArtefact` below is the one caller left on the manual fetch path: a 404
 * there is a legitimate "not started yet" result, not an error, and it has to
 * inspect the response *before* deciding whether to throw — which
 * `createRequest`'s `onError` (bound once, for every call `request` makes)
 * cannot do per-call. Kept only for that one case. */
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

async function throwForResponse(response: Response, context: string): Promise<never> {
  if (response.status === 401) {
    throw new Error('not signed in (401) — sign in to the portal, then reload')
  }
  const detail = await detailMessage(response)
  if (response.status === 403) {
    throw new Error(detail !== null ? `${detail} (403)` : `you need the admin role to ${context} (403)`)
  }
  const message = detail !== null ? `${detail} (${response.status})` : `could not ${context} (${response.status})`
  // Issue #159: the run itself failed, so the operator needs a re-run rather
  // than another read. Same message, a different type — see
  // {@link StageRunFailedError} for why 502 specifically.
  if (response.status === 502) {
    throw new StageRunFailedError(message)
  }
  throw new Error(message)
}

/** Update a campaign's own fields directly — generated CRUD, not an admin-app
 * route. Used here for `brief`: nothing in the create form collects one (see
 * `App.tsx`), and without a brief `start_research_route` 422s, so the
 * pipeline's first stage can never start. */
export async function updateCampaign(
  campaignId: string,
  patch: Partial<
    Pick<
      Campaign,
      'name' | 'destination_url' | 'brief' | 'guidance' | 'motion' | 'target_channel_keys'
    >
  >,
): Promise<Campaign> {
  return request<Campaign>('PATCH', `/campaigns/${campaignId}`, patch, BASE, 'update the campaign')
}

// ── The pipeline (M3/M4/M5): research → positioning → channel plan → copy ───

export interface Source {
  url: string
  note: string
}

/** `id` is server-assigned at persist time (`pipeline.with_element_ids`,
 * issue #145) — never model-generated, never positional. **Optional**, not
 * required: an artefact body written before #150 shipped has none, and
 * carries none until it is re-run. Every element written from here on
 * always has one; the optionality only covers that legacy gap, and the
 * selection UI (`PipelineStage`/`ArtefactBody`) treats an id-less element as
 * not independently selectable — it rides along with whatever the approval
 * decides for the rest of the artefact, exactly as it did before this issue. */
export interface ResearchFinding {
  id?: string
  signal: string
  why_it_matters: string
  sources: Source[]
}

export interface ResearchSynthesisBody {
  summary: string
  findings: ResearchFinding[]
}

export interface Segment {
  id?: string
  name: string
  description: string
  sources: Source[]
}

export interface MessagePillar {
  id?: string
  pillar: string
  rationale: string
  sources: Source[]
}

export interface CallToAction {
  id?: string
  text: string
  rationale: string
  sources: Source[]
}

export interface PositioningBody {
  segments: Segment[]
  pillars: MessagePillar[]
  ctas: CallToAction[]
}

/** #76 increment 2: exactly one of `channel_key`/`suggested_label` is set —
 * a `channel_key` from the taxonomy, or a free-text proposal for a channel
 * outside it (#67). Never both, never neither — enforced server-side by
 * `definitions.ChannelRecommendation`'s own validator, not re-checked here. */
export interface ChannelRecommendation {
  id?: string
  channel_key: string | null
  suggested_label: string | null
  motion: 'organic' | 'paid'
  rank: number
  rationale: string
  sources: Source[]
}

export interface ChannelPlanBody {
  channels: ChannelRecommendation[]
}

/** #76 increment 2: `channel` → `channel_key`, always a real taxonomy key —
 * a proposal is never promoted into copy without operator acceptance, so
 * `ChannelCopy` (unlike `ChannelRecommendation`) has no `suggested_label`. */
/** One copy field that came back longer than its length budget (#128).
 * Derived server-side in `pipeline.measure_copy_length`, never written by the
 * agent — and it carries its own `budget`, so this UI renders the finding
 * without a second copy of numbers that live in `definitions.py`. */
export interface LengthOverage {
  field: 'headline' | 'body' | 'cta'
  length: number
  budget: number
}

export interface ChannelCopy {
  id?: string
  channel_key: string
  motion: 'organic' | 'paid'
  headline: string
  body: string
  cta: string
  sources: Source[]
  /** Absent on any copy artefact proposed before #128 landed — every reader
   * must treat it as optional rather than assume the server filled it. */
  over_budget?: LengthOverage[]
}

export interface CopySetBody {
  channels: ChannelCopy[]
}

/** One `marketing_channel` taxonomy row (#67, #76) — `list`/`read` open to
 * any authenticated tenant caller (manifest note on `marketing_channel`),
 * which is what lets this UI resolve a `channel_key` to its operator-facing
 * `label` without an admin-only round trip. */
export interface ChannelTaxonomyEntry {
  key: string
  label: string
  motion: 'organic' | 'paid'
  category: string
  ad_platform: string | null
  /** Where an operator actually goes to publish on this channel — LinkedIn's
   * post composer, Google Ads, and so on (#103b). `null` for channels with
   * no single composer (a pitch to a publication, a tenant-specific email
   * tool) — render nothing, never a placeholder or dead link. */
  publish_url: string | null
}

export async function listChannels(): Promise<ChannelTaxonomyEntry[]> {
  const body = await request<unknown>('GET', '/channels')
  return Array.isArray(body) ? (body as ChannelTaxonomyEntry[]) : []
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

/** Approve a `proposed` artefact, optionally narrowed to a subset of its
 * elements (issue #145).
 *
 * `elementIds` is `null`/omitted for "approve everything" — the ONLY body
 * this function sent before #145, and the one every existing caller must
 * keep sending unchanged: `undefined` here means no request body goes out at
 * all (`api-core.ts`'s `request` only serialises a body when one is passed),
 * matching `approve_artefact_route`'s own "no body = approve everything"
 * reading. A non-empty array narrows the approval to exactly those ids; the
 * route itself rejects an empty array (422) — see {@link StaleArtefactError}
 * for the other 422 this call can throw, which is a different failure and
 * needs a different response from the caller. */
export async function approveArtefact(
  campaignId: string,
  kind: ArtefactKind,
  elementIds?: string[] | null,
): Promise<Artefact> {
  return request<Artefact>(
    'POST',
    `/campaigns/${campaignId}/artefacts/${kind}/approve`,
    elementIds != null ? { element_ids: elementIds } : undefined,
    ADMIN_BASE,
    `approve the ${kind.replace('_', ' ')} stage`,
  )
}

export async function rejectArtefact(campaignId: string, kind: ArtefactKind): Promise<Artefact> {
  return request<Artefact>(
    'POST',
    `/campaigns/${campaignId}/artefacts/${kind}/reject`,
    undefined,
    ADMIN_BASE,
    `reject the ${kind.replace('_', ' ')} stage`,
  )
}

export async function startResearch(campaignId: string): Promise<Artefact> {
  return request<Artefact>('POST', `/campaigns/${campaignId}/research`, undefined, ADMIN_BASE, 'start research')
}

export async function startPositioning(campaignId: string): Promise<Artefact> {
  return request<Artefact>(
    'POST',
    `/campaigns/${campaignId}/positioning`,
    undefined,
    ADMIN_BASE,
    'start positioning',
  )
}

export async function startChannelPlan(campaignId: string): Promise<Artefact> {
  return request<Artefact>(
    'POST',
    `/campaigns/${campaignId}/channel-plan`,
    undefined,
    ADMIN_BASE,
    'start channel planning',
  )
}

export async function startCopy(campaignId: string): Promise<Artefact> {
  return request<Artefact>('POST', `/campaigns/${campaignId}/copy`, undefined, ADMIN_BASE, 'start copy generation')
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
  return request<GenerateStillResponse>(
    'POST',
    `/campaigns/${campaignId}/stills`,
    { prompt },
    ADMIN_BASE,
    'generate an image',
  )
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

/** The assets half of the pack, on its own — no copy gate, no link minting.
 * `pack_routes.list_assets_route` explains why this is a separate route
 * rather than a `/pack` call: `/pack` 404s/409s until copy is approved and
 * mints tracked links as a side effect, neither of which a panel that loads
 * on mount can accept. */
export interface CampaignAssets {
  campaign_id: string
  assets: PackAsset[]
  missing_placements: string[]
  superseded_source_count: number
}

export async function getCampaignAssets(campaignId: string): Promise<CampaignAssets> {
  return request<CampaignAssets>(
    'GET',
    `/campaigns/${campaignId}/assets`,
    undefined,
    ADMIN_BASE,
    "load this campaign's existing creative",
  )
}

export async function getPack(campaignId: string): Promise<Pack> {
  return request<Pack>('GET', `/campaigns/${campaignId}/pack`, undefined, ADMIN_BASE, 'load the distribution pack')
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

/** One segment as it appears in the paid pack's targeting brief —
 * `paid_pack_routes.py`'s `_targeting_segment`. Deliberately NOT `Segment`:
 * the full `sources` (`{url, note}` pairs) stay on the positioning artefact,
 * where an operator reviews evidence. Repeating them on every targeting
 * entry too was the reported duplication — the same handful of research
 * URLs and notes on every artefact that touched a segment. `source_count`
 * keeps the "this is evidenced" signal without the repetition. */
export interface TargetingSegment {
  name: string
  description: string
  source_count: number
}

export interface PaidPack {
  campaign_id: string
  ad_copy: AdCopyVariant[]
  assets: PackAsset[]
  missing_placements: string[]
  targeting: TargetingSegment[]
  budget: BudgetRecommendation
  links: PackLink[]
  guidance: string
  /** What an operator has recorded spending on this campaign (issue #8), or
   * the reason there is no figure. See {@link SpendMetric}.
   *
   * **Deliberately not an unmeasurable-only type.** It used to be one, back
   * when spend genuinely could not exist — and once `recordSpend` made it
   * possible, that type would have left a measured figure literally
   * unrepresentable, which is issue #114 exactly: the results dashboard
   * rendered a real leads figure as "not measurable — undefined" for that
   * same reason. */
  spend: SpendMetric
}

/** The paid pack's `spend`: `results_routes.Metric`'s two shapes, plus the
 * currency a measured value is denominated in (`spend_routes.SpendMetric`).
 * `240.5` alone is not a spend figure — 240.5 of what? — and `currency` is
 * `null` exactly when `measurable` is `false`, for the same reason `value`
 * is: there is no figure to denominate.
 *
 * A campaign with nothing recorded is `measurable: false`, NOT a zero: this
 * plugin calls no ad platform API, so no rows means nobody has entered any,
 * which is not the same as having spent nothing. A recorded `0` is
 * `measurable: true, value: 0` — a real, measured zero. Render the two
 * differently. */
export type SpendMetric =
  | (MeasuredMetric & { currency: string })
  | (UnmeasurableMetric & { currency: null })

export async function getPaidPack(campaignId: string): Promise<PaidPack> {
  return request<PaidPack>('GET', `/campaigns/${campaignId}/paid-pack`, undefined, ADMIN_BASE, 'load the paid pack')
}

/** Record one spend entry against a campaign (`spend_routes.record_spend_route`).
 *
 * Additive: each call records what was spent, and the paid pack totals every
 * entry. `currency` defaults to the currency the pack states its budget
 * recommendation in, so the plan and the actual are comparable unless an
 * operator says otherwise; the server upper-cases it, so `usd` and `USD` do
 * not become two currencies that the total then refuses to add.
 */
export async function recordSpend(
  campaignId: string,
  entry: { amount: number; currency?: string; notes?: string | null },
): Promise<void> {
  await request<unknown>(
    'POST',
    `/campaigns/${campaignId}/spend`,
    { amount: entry.amount, currency: entry.currency ?? 'USD', notes: entry.notes ?? null },
    ADMIN_BASE,
    'record spend',
  )
}

// ── Results (M8) ──────────────────────────────────────────────────────────────

export interface ClickBreakdown {
  total: number
  paid: number
  organic: number
  unknown_channel_type: number
}

/* `UnmeasuredMetric` used to live here — a metric shape whose `measurable`
 * was fixed at `false`, mirroring `results_routes.UnmeasuredMetric`. Issue
 * #31 moved leads/conversions/cost off it and issue #8 moved the paid pack's
 * `spend` off it, so nothing is that shape any more and the server no longer
 * defines it. Deleted rather than kept available: a type that can only be
 * unmeasurable makes the measured case unrepresentable, which is precisely
 * how #114 shipped a real leads figure as "not measurable — undefined". */

/** A leads/conversions/cost figure the instance-configured leads source
 * answered (issue #31). Mirrors `results_routes.py`'s `Metric` when
 * `measurable` is `True`: `value` is a real number — a real zero is
 * `value: 0, measurable: true`, distinguishable from unmeasurable by
 * `measurable` alone and never by `value` being falsy — and `reason` is
 * `null`, because there is nothing to excuse. */
export interface MeasuredMetric {
  value: number
  measurable: true
  denominator: number | null
  reason: null
}

/** The other half of `results_routes.py`'s `Metric`: the source could not
 * answer, and `reason` says why, verbatim from upstream where it gave one.
 * `value` is `null` — the server sends the key, and it is never a zero. */
export interface UnmeasurableMetric {
  value: null
  measurable: false
  denominator: number | null
  reason: string
}

/** Exactly the union `results_routes.py`'s `Metric` serialises, discriminated
 * on `measurable`. Typing only the unmeasurable half made the measurable one
 * unrepresentable, so the dashboard rendered a real leads figure as "not
 * measurable — undefined" (issue #114). Both halves always carry
 * `denominator`, measurable or not. */
export type Metric = MeasuredMetric | UnmeasurableMetric

export interface CampaignResults {
  campaign_id: string
  campaign_name: string
  clicks: ClickBreakdown
  leads: Metric
  conversions: Metric
  cost: Metric
}

export interface ResultsResponse {
  campaigns: CampaignResults[]
  /** Clicks whose `campaign_id` named no campaign this call could see —
   * counted rather than silently dropped from every campaign's total. */
  unattributed_clicks: number
}

export async function getResults(): Promise<ResultsResponse> {
  return request<ResultsResponse>('GET', '/results', undefined, ADMIN_BASE, 'load results')
}

// ── The fan-in workflow, and re-seeding it from here (issue #160) ────────────
//
// Two different APIs, on purpose. The DECLARED definition comes from the
// plugin's own admin app, which reads it straight out of `definitions.py`. The
// DEPLOYED one comes from Core's orchestration API, which is admin-Cognito
// gated — and that gate is exactly why this lives in the browser: the plugin's
// Lambda signs its Core calls with SigV4, which those routes do not accept, so
// the operator's own session is the only credential in the system that can
// both read and replace a workflow definition.

/** Core's workflow-definition CRUD. NOT under `/admin` — that variant 404s,
 * indistinguishable from a route that does not exist, which is how the seed
 * script silently never seeded anything until 2026-08-11. */
const CORE_ORCHESTRATION_BASE = '/api/v1/orchestration'

/** The fan-in workflow definition this build declares. */
export interface DeclaredWorkflow {
  name: string
  definition: {
    name: string
    trigger_source: string
    trigger_detail_type: string
    action_type: string
    action_config: Record<string, unknown>
    enabled: boolean
  }
  fingerprint: string
}

/** One workflow definition as Core holds it. Only the fields this panel reads
 * are declared; Core returns more. */
export interface DeployedWorkflow {
  id: string
  name: string
  action_config: Record<string, unknown> | null
}

export async function getDeclaredFanInWorkflow(): Promise<DeclaredWorkflow> {
  return request<DeclaredWorkflow>(
    'GET',
    '/fan-in-workflow',
    undefined,
    ADMIN_BASE,
    'read the declared fan-in workflow',
  )
}

export async function listCoreWorkflows(): Promise<DeployedWorkflow[]> {
  const body = await request<unknown>(
    'GET',
    '/workflows',
    undefined,
    CORE_ORCHESTRATION_BASE,
    'list the deployed workflows',
  )
  return Array.isArray(body) ? (body as DeployedWorkflow[]) : []
}

/** Write the declared definition to Core — replacing the existing one when
 * `id` is given, creating it when it is not.
 *
 * The create/replace split is the seed script's, kept because the two are not
 * interchangeable: POSTing over an existing name is what produces two
 * definitions firing on the same trigger, and the engine would then run
 * synthesis twice per chain. */
export async function seedFanInWorkflow(
  declared: DeclaredWorkflow,
  existingId: string | null,
): Promise<void> {
  if (existingId) {
    await request<unknown>(
      'PUT',
      `/workflows/${existingId}`,
      declared.definition,
      CORE_ORCHESTRATION_BASE,
      're-seed the fan-in workflow',
    )
    return
  }
  await request<unknown>(
    'POST',
    '/workflows',
    declared.definition,
    CORE_ORCHESTRATION_BASE,
    'seed the fan-in workflow',
  )
}

/** Making the open campaign addressable, so a studio view can be shared (#142).
 *
 * ## Why a query parameter and not a path
 *
 * The obvious shape — `…/admin/campaigns/<id>` — cannot work here, and the
 * reason is structural rather than a matter of taste. The admin SPA is served
 * by the same ASGI app that serves the admin API, as a `StaticFiles` mount at
 * `/` registered **after** every route (see `admin_app.py`'s module docstring:
 * a mount at `/` swallows everything registered after it, so it must go last).
 *
 * `/campaigns/{campaign_id}` is one of those earlier routes. So a browser
 * navigation to that path never reaches the SPA — it matches the API route,
 * the gateway's JWT authorizer answers first, and the reader gets:
 *
 *     {"message": "Unauthorized"}
 *
 * because a navigation carries no bearer token. Verified live on tabsii dev.
 *
 * A query parameter is invisible to routing on both sides: the path stays
 * `…/admin`, which is the path the shell actually answers on, and the id rides
 * along where only the client reads it.
 *
 * A fragment (`#/campaigns/<id>`) would also avoid the collision — it is never
 * sent to the server at all — but it is invisible to any server-side logging
 * and reads as dated. The query string is the better default; nothing here
 * prevents revisiting that.
 *
 * ## Why the id is validated rather than trusted
 *
 * The value is attacker-controllable in the sense that anyone can paste
 * anything into a URL bar. It is only ever compared against ids already
 * fetched for this tenant, so a junk value can at worst fail to match — but
 * validating the shape means junk is rejected before it reaches a comparison
 * or is echoed back into `history`, and keeps "not a campaign id" separate
 * from "a campaign id you cannot see".
 */

/** The query parameter carrying the open campaign's id. */
export const CAMPAIGN_PARAM = 'campaign'

//: A campaign id is a UUID (`marketing_campaign.id`). Deliberately shape-only:
//: this says "could be an id", never "is a campaign you may see" — that
//: question belongs to the tenant-scoped fetch, not to a regex.
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/**
 * The campaign id a URL is asking for, or `null` if it is not asking for one.
 *
 * `search` is the raw query string (`window.location.search`). A malformed or
 * non-UUID value returns `null` rather than throwing: a bad link should land
 * the reader on the campaign list, which is a sensible place to be, not on an
 * error page.
 */
export function readCampaignParam(search: string): string | null {
  const raw = new URLSearchParams(search).get(CAMPAIGN_PARAM)
  if (raw === null) return null
  const trimmed = raw.trim()
  return UUID.test(trimmed) ? trimmed : null
}

/**
 * The URL for a given open campaign, preserving everything else about the
 * current location.
 *
 * Takes `pathname` and `search` rather than reading `window` so it is testable
 * without a DOM, and so it **cannot change the path**: the shell answers on
 * `…/admin` and a trailing slash 401s, so the path is not this function's to
 * rewrite.
 *
 * Passing `null` for `id` removes the parameter, which is what closing a
 * campaign should do — leaving a stale `?campaign=` on the list view would
 * make the address bar disagree with the screen.
 *
 * Other parameters are preserved rather than dropped: this is one feature's
 * parameter, not the whole query string's owner.
 */
export function campaignUrl(id: string | null, pathname: string, search: string): string {
  const params = new URLSearchParams(search)
  if (id === null) {
    params.delete(CAMPAIGN_PARAM)
  } else {
    params.set(CAMPAIGN_PARAM, id)
  }
  const query = params.toString()
  return query === '' ? pathname : `${pathname}?${query}`
}

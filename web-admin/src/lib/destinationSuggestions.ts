import type { Campaign } from './api'

/** Distinct destination URLs this tenant has already used, for the new-campaign
 * form's suggestion list (#129).
 *
 * Derived entirely from `campaigns` — the same array `App.tsx` already fetches
 * via `listCampaigns()` to render the table below the form. No new route:
 * `marketing_campaign.destination_url` on the rows already on the page is the
 * whole source, and a plugin route is a new surface to secure, test and
 * version for data that is already in hand.
 *
 * Ordered by how often a URL appears, most-used first — NOT by recency.
 * Recency would need a `created_at` per campaign, and `Campaign` (`api.ts`)
 * carries none; more fundamentally, Core's generic list-route order "carries
 * no `ORDER BY` and is not guaranteed stable" (`pack_routes.py`'s own
 * `_link_sort_key` docstring, of the very `/campaigns` list this reads), so
 * treating array position as recency would be reading a promise the API does
 * not make. Frequency is real, and needs nothing from the API beyond what
 * `listCampaigns` already returns. Ties keep first-seen order, so the result
 * is stable across renders for a fixed `campaigns` array.
 */
export function destinationSuggestions(campaigns: Campaign[]): string[] {
  const counts = new Map<string, number>()
  for (const c of campaigns) {
    const url = c.destination_url?.trim()
    if (url === undefined || url === '') continue
    counts.set(url, (counts.get(url) ?? 0) + 1)
  }
  return [...counts.keys()].sort((a, b) => counts.get(b)! - counts.get(a)!)
}

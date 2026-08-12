import type { PackAsset, PackLink } from '../lib/api'
import { assetFilename } from '../lib/assetFilename'
import { DownloadButton } from './DownloadButton'

/** The organic pack (M5) and paid pack (M9) are the same shape with
 * paid-specific additions on top (`paid_pack_routes.py`'s own module
 * docstring: "the organic pack's shape... not a second assembler"). These
 * four pieces — the missing-placements warning, the creative grid, the
 * tracked-links list, and the guidance block — render identically in both,
 * so they live here once rather than twice. A fix to the clipboard-copy
 * failure path (see `useClipboard`) or to how a missing render is disclosed
 * now lands in one place for both packs, not one and possibly not the
 * other.
 *
 * #117's download affordance is why that matters concretely: the creative
 * grid is one component, so both packs gained the ability to save an asset
 * in the same change rather than the organic pack getting it and the paid
 * pack silently keeping a bare `<img>`.
 */

export function MissingPlacementsWarning({ missingPlacements }: { missingPlacements: string[] }) {
  if (missingPlacements.length === 0) return null
  return (
    <p className="warning">
      Missing renders for: {missingPlacements.join(', ')} — only the source creative is available
      for these placements so far.
    </p>
  )
}

/** What one asset is, in the operator's words — the label under the
 * thumbnail and the thing the download control names. `is_source` and
 * `placement` are the only two things that distinguish assets in a pack
 * (`pack_routes._existing_assets`). */
function assetLabel(a: PackAsset): string {
  if (a.is_source === true) return 'source'
  return a.placement ?? 'creative'
}

/** The creative grid, with a save affordance per asset (#117).
 *
 * `campaignName` exists solely to name the downloaded file — object storage
 * knows the bytes only as a uuid, so without it the operator's camera roll
 * fills with indistinguishable `9f1c….png`s. See `lib/assetFilename.ts` for
 * how the name is composed and `DownloadButton.tsx` for why this is a plain
 * link rather than a fetch-to-blob.
 */
export function PackAssets({
  assets,
  campaignName,
}: {
  assets: PackAsset[]
  campaignName: string
}) {
  return (
    <>
      <h4>Creative</h4>
      {assets.length === 0 && <p className="empty">No creative yet.</p>}
      <ul className="assets">
        {assets.map((a) => (
          <li key={a.id}>
            <img src={a.url} alt={a.placement ?? 'Source creative'} />
            <span>{a.is_source === true ? 'Source' : (a.placement ?? '—')}</span>
            <DownloadButton
              url={a.url}
              filename={assetFilename(a, campaignName)}
              accessibleName={`Download ${assetLabel(a)} creative`}
            />
          </li>
        ))}
      </ul>
    </>
  )
}

export function PackLinks({
  links,
  copied,
  onCopy,
}: {
  links: PackLink[]
  copied: string | null
  onCopy: (url: string) => void
}) {
  return (
    <>
      <h4>Tracked links</h4>
      {links.length === 0 && <p className="empty">No tracked links yet.</p>}
      <ul className="minted">
        {links.map((l, i) => (
          <li key={`${l.channel}-${i}`}>
            <span className="chan">
              {l.channel}
              {l.variant !== null && l.variant !== '' ? ` · ${l.variant}` : ''}
              {l.is_paid === true ? ' · paid' : ''}
            </span>
            {l.url !== null ? (
              <>
                <code>{l.url}</code>
                <button type="button" onClick={() => onCopy(l.url as string)}>
                  {copied === l.url ? 'Copied' : 'Copy'}
                </button>
              </>
            ) : (
              <span className="empty">No public base URL configured for this deployment</span>
            )}
          </li>
        ))}
      </ul>
    </>
  )
}

export function PackGuidance({ guidance }: { guidance: string }) {
  if (guidance === '') return null
  return (
    <>
      <h4>Guidance</h4>
      <p className="guidance">{guidance}</p>
    </>
  )
}

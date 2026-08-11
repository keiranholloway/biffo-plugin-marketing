import type { PackAsset, PackLink } from '../lib/api'

/** The organic pack (M5) and paid pack (M9) are the same shape with
 * paid-specific additions on top (`paid_pack_routes.py`'s own module
 * docstring: "the organic pack's shape... not a second assembler"). These
 * four pieces — the missing-placements warning, the creative grid, the
 * tracked-links list, and the guidance block — render identically in both,
 * so they live here once rather than twice. A fix to the clipboard-copy
 * failure path (see `useClipboard`) or to how a missing render is disclosed
 * now lands in one place for both packs, not one and possibly not the
 * other.
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

export function PackAssets({ assets }: { assets: PackAsset[] }) {
  return (
    <>
      <h4>Creative</h4>
      {assets.length === 0 && <p className="empty">No creative yet.</p>}
      <ul className="assets">
        {assets.map((a) => (
          <li key={a.id}>
            <img src={a.url} alt={a.placement ?? 'Source creative'} />
            <span>{a.is_source === true ? 'Source' : (a.placement ?? '—')}</span>
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

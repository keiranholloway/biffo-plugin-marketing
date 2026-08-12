import type { PackAsset } from './api'

/** The filename a downloaded creative arrives under.
 *
 * The bytes are named by a uuid in object storage (`image_provider.py`:
 * `filename=f"{uuid.uuid4()}.png"`), and Core presigns every GET with
 * `Content-Disposition: attachment; filename="<that uuid>"`
 * (`plugin_storage.presign_download`). An operator who saves three
 * placements for two campaigns therefore ends up with six indistinguishable
 * uuids in their camera roll. This composes the name a person can actually
 * pick out of a list — `<campaign>-<placement>.<ext>` — for the `download`
 * attribute to ask for instead.
 *
 * Pure, and deliberately separate from the component: the filename is the
 * part of #117 a test can genuinely prove, so it should not need a DOM to
 * exercise.
 */

/** Cap on the campaign portion of a filename. Long enough to stay
 * recognisable, short enough that the whole name survives a mobile
 * filesystem and a share sheet. */
const MAX_SLUG = 60

/**
 * A lowercase, path-safe, hyphen-separated form of `value` — `''` if there
 * is nothing sluggable in it, so a caller can fall back rather than emit a
 * filename beginning with a stray separator.
 *
 * Accents are folded rather than dropped (`Café` → `cafe`, not `caf`), so a
 * non-ASCII campaign name still produces something recognisable.
 */
export function slugify(value: string): string {
  return value
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, MAX_SLUG)
    .replace(/-+$/g, '')
}

/**
 * The file extension implied by a presigned URL's *path*, ignoring its query.
 *
 * The query is where the whole SigV4 presign lives (`X-Amz-Signature`,
 * `X-Amz-Expires`, …), so a naive "text after the last dot" reads the tail of
 * a signature as an extension. Anything that is not a short alphanumeric run
 * falls back to `png`, which is what this plugin's own generation path
 * produces (`image_provider.py`).
 */
function extensionFrom(url: string): string {
  try {
    // A base is supplied so a relative or malformed URL still parses rather
    // than throwing — the host is never read.
    const { pathname } = new URL(url, 'https://asset.invalid')
    const last = pathname.slice(pathname.lastIndexOf('/') + 1)
    const dot = last.lastIndexOf('.')
    if (dot > 0) {
      const ext = last.slice(dot + 1).toLowerCase()
      if (/^[a-z0-9]{2,4}$/.test(ext)) return ext
    }
  } catch {
    // Fall through to the default — an unparseable URL is still downloadable.
  }
  return 'png'
}

/**
 * `<campaign>-<placement|source>.<ext>` for one pack asset.
 *
 * `is_source` and `placement` are the only two things that distinguish one
 * asset from another in a pack (`pack_routes._existing_assets` guarantees at
 * most one source row survives), so they are what the name has to carry.
 */
export function assetFilename(asset: PackAsset, campaignName: string): string {
  const campaign = slugify(campaignName) || 'campaign'
  const placement = slugify(asset.placement ?? '')
  const part = placement || (asset.is_source === true ? 'source' : 'creative')
  return `${campaign}-${part}.${extensionFrom(asset.url)}`
}

/** A save affordance for one creative asset (#117), sitting alongside
 * `CopyButton` as the other half of "get the pack's contents out of the
 * browser" — the distribution pack exists so an operator standing in a venue
 * with a phone can publish from it, and until this the only way to save the
 * image was to long-press it and hope.
 *
 * ## Why a plain `<a>` and not a fetch-to-blob
 *
 * The obvious implementation — `fetch(url)`, `URL.createObjectURL(blob)`,
 * click a synthetic `<a download>` — is the one that cannot work here, and
 * it fails invisibly in a way jsdom cannot see:
 *
 *   - The asset URL is a **presigned S3 URL**, minted per read by
 *     `pack_routes._asset_with_url`. It is cross-origin to this app, so the
 *     fetch is a CORS request.
 *   - The bucket's CORS rule takes its origins from Terraform's
 *     `plugin_media_cors_origins` (core: `modules/cloud/aws/storage/main.tf`),
 *     which **defaults to `[]` and is not set in dev, staging or prod**. No
 *     origin is permitted, so that fetch is refused by the browser.
 *
 * The other candidate — proxying the bytes through a plugin endpoint that
 * sets its own `Content-Disposition` — is ruled out by this plugin's own
 * history: `pack_routes.py`'s module docstring records that fetching a
 * presigned URL server-side is exactly the `py/full-ssrf` sink CodeQL flags,
 * that **four** different mitigation shapes failed to clear it, and that the
 * fetch was deliberately removed. Reintroducing it to add a download button
 * would trade a missing affordance for a blocked security gate.
 *
 * ## Why a plain `<a>` nevertheless saves the file
 *
 * Core already presigns every GET with
 * `Content-Disposition: attachment; filename="<stored filename>"`
 * (`plugin_storage.presign_download`). A response header outranks anything
 * the markup asks for and needs no CORS at all, so following this link
 * downloads rather than navigates.
 *
 * The `download` attribute is therefore **not** what makes the save happen —
 * it is what makes the save *well named*. It is ignored on a cross-origin
 * URL, in which case the file lands under Core's stored filename (a uuid);
 * where the browser honours it, the operator gets
 * `<campaign>-<placement>.png` (`lib/assetFilename.ts`). Degrading is the
 * point: the file is saved either way.
 *
 * `target="_blank"` is not cosmetic. A presign is short-lived, and a pack
 * sits on screen after loading — following an expired one in the same tab
 * would replace the pack with S3's XML error document. `rel="noreferrer"`
 * keeps the signed URL out of any Referer header and withholds the
 * `window.opener` handle.
 */
export function DownloadButton({
  url,
  filename,
  label = 'Download',
  accessibleName,
  className = '',
}: {
  url: string
  filename: string
  label?: string
  /** Overrides the accessible name — a grid of assets otherwise renders
   * several links all called "Download". */
  accessibleName?: string
  className?: string
}) {
  return (
    <a
      className={`download-btn secondary ${className}`.trim()}
      href={url}
      download={filename}
      target="_blank"
      rel="noreferrer"
      aria-label={accessibleName}
    >
      {label}
    </a>
  )
}

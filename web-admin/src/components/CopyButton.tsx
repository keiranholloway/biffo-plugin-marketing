/** A copy affordance for one piece of pack text, backed by the shared
 * `useClipboard` hook (`useClipboard.ts`) rather than a second clipboard
 * implementation — the same hook `MintLinks`/`PackLinks` already use for
 * tracked links now backs every copy block in both distribution packs too
 * (#103a).
 *
 * `copied`/`onCopy` are threaded down from the caller's own single
 * `useClipboard()` call (`DistributionPack`, `PaidPack`) — one clipboard
 * state shared across every copy control a pack renders, the same sharing
 * `PackLinks` already does for tracked-link copying, not one hook instance
 * per button.
 *
 * A native `<button>` gets keyboard focus and activation, and the visible
 * focus ring, for free from `index.css`'s existing `button:focus-visible`
 * rule — nothing new needed there for this control to be keyboard-accessible.
 */
export function CopyButton({
  text,
  label = 'Copy',
  copiedLabel = 'Copied',
  copied,
  onCopy,
  className = '',
}: {
  text: string
  label?: string
  copiedLabel?: string
  copied: string | null
  onCopy: (text: string) => void
  className?: string
}) {
  return (
    <button
      type="button"
      className={`copy-btn secondary ${className}`.trim()}
      onClick={() => onCopy(text)}
    >
      {copied === text ? copiedLabel : label}
    </button>
  )
}

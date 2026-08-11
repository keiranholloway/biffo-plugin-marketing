import { useState } from 'react'

/** Copy text to the clipboard, tracking which string was last copied
 * successfully so a caller can render "Copied" next to the right item.
 *
 * A clipboard write can be silently refused — permissions, or Safari outside
 * a user gesture (`pack_routes.py`'s module docstring names this explicitly
 * as one of M5's two unverified day-0 device warnings). Shared here rather
 * than duplicated per pack view: a fix to this failure path belongs in one
 * place, not in however many components render a "Copy" button.
 */
export function useClipboard() {
  const [copied, setCopied] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(text)
      setError(null)
    } catch {
      // The text is still on screen and selectable either way — say so
      // rather than showing a success that did not happen.
      setCopied(null)
      setError('could not copy — select the text and copy it manually')
    }
  }

  return { copied, copy, error }
}

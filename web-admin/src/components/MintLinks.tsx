import { useState } from 'react'

import { mintLinks, type MintedLink } from '../lib/api'

/** Mint tracked links for one campaign, and show the URLs to publish.
 *
 * The operator supplies a channel and, optionally, a variant and whether the
 * link is paid. Everything that makes the link *trackable* — the token, and the
 * destination carrying `utm_campaign` — is derived server-side and deliberately
 * not offered here: a hand-typed `utm_campaign` is precisely what this feature
 * exists to remove.
 */
export function MintLinks({ campaignId }: { campaignId: string }) {
  const [channel, setChannel] = useState('')
  const [variant, setVariant] = useState('')
  const [isPaid, setIsPaid] = useState(false)
  const [minting, setMinting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [minted, setMinted] = useState<MintedLink[]>([])
  const [copied, setCopied] = useState<string | null>(null)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setMinting(true)
    setError(null)
    try {
      const links = await mintLinks(campaignId, [
        {
          channel: channel.trim(),
          ...(variant.trim() !== '' ? { variant: variant.trim() } : {}),
          is_paid: isPaid,
        },
      ])
      setMinted((prev) => [...prev, ...links])
      setChannel('')
      setVariant('')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setMinting(false)
    }
  }

  async function copy(url: string) {
    try {
      await navigator.clipboard.writeText(url)
      setCopied(url)
    } catch {
      // A clipboard write can be refused (permissions, or Safari outside a
      // user gesture). Say so rather than showing a success that did not
      // happen — the URL is on screen and selectable either way.
      setCopied(null)
      setError('could not copy — select the URL and copy it manually')
    }
  }

  return (
    <div className="mint">
      <form onSubmit={submit} aria-label={`Mint a tracked link for ${campaignId}`}>
        <label htmlFor={`channel-${campaignId}`}>Channel</label>
        <input
          id={`channel-${campaignId}`}
          value={channel}
          onChange={(e) => setChannel(e.target.value)}
          placeholder="linkedin"
        />

        <label htmlFor={`variant-${campaignId}`}>Variant (optional)</label>
        <input
          id={`variant-${campaignId}`}
          value={variant}
          onChange={(e) => setVariant(e.target.value)}
          placeholder="a"
        />

        <label className="checkbox">
          <input type="checkbox" checked={isPaid} onChange={(e) => setIsPaid(e.target.checked)} />
          Paid placement
        </label>

        <button type="submit" disabled={channel.trim() === '' || minting}>
          {minting ? 'Minting…' : 'Mint link'}
        </button>
      </form>

      {error !== null && <p className="error">{error}</p>}

      {minted.length > 0 && (
        <ul className="minted">
          {minted.map((link) => (
            <li key={link.id}>
              <span className="chan">
                {link.channel}
                {link.variant ? ` · ${link.variant}` : ''}
                {link.is_paid ? ' · paid' : ''}
              </span>
              <code>{link.url}</code>
              <button type="button" onClick={() => copy(link.url)}>
                {copied === link.url ? 'Copied' : 'Copy'}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

import { useState } from 'react'

import { mintLinks, type MintedLink } from '../lib/api'
import type { ChannelLookup } from '../lib/useChannelTaxonomy'

/** Mint tracked links for one campaign, and show the URLs to publish.
 *
 * The operator picks a channel from the seeded taxonomy and, optionally,
 * types a variant. Everything that makes the link *trackable* — the token,
 * and the destination carrying `utm_campaign` — is derived server-side and
 * deliberately not offered here: a hand-typed `utm_campaign` is precisely
 * what this feature exists to remove.
 *
 * #84: this used to be a free-text channel input, so `"totally made up
 * channel"` minted successfully into the same `marketing_link.channel`
 * column `pack_routes._ensure_links` fills with taxonomy keys — splitting
 * one column into two vocabularies written by two paths. A picker makes a
 * non-key value structurally impossible to submit here, rather than
 * rejecting one after the fact with nothing telling the operator what the
 * valid values are (rejecting free text alone would still be a worse form
 * than today's, per the issue: it would turn a working form into a failing
 * one without saying what to type instead). `channelLookup` is fetched once
 * by the caller (`App.tsx`) via `useChannelTaxonomy` and threaded down here
 * — the same sharing `CampaignDetail` already does for `Pipeline`/
 * `DistributionPack` — rather than this component fetching its own copy.
 *
 * No "Paid placement" checkbox any more, for the same reason: motion now
 * lives on the channel (#76 increment 2), so picking `linkedin_paid` already
 * says the link is paid. A second, independently-set checkbox was a second
 * answer to the same question — `admin_app.mint_links` now derives `is_paid`
 * from the selected channel's own taxonomy row and does not accept one from
 * the request at all, so offering the checkbox here would just be a control
 * the server silently ignores.
 */
export function MintLinks({
  campaignId,
  channelLookup,
}: {
  campaignId: string
  channelLookup: ChannelLookup
}) {
  const [channelKey, setChannelKey] = useState('')
  const [variant, setVariant] = useState('')
  const [minting, setMinting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [minted, setMinted] = useState<MintedLink[]>([])
  const [copied, setCopied] = useState<string | null>(null)

  const selected = channelLookup.get(channelKey)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setMinting(true)
    setError(null)
    try {
      const links = await mintLinks(campaignId, [
        {
          channel: channelKey,
          ...(variant.trim() !== '' ? { variant: variant.trim() } : {}),
        },
      ])
      setMinted((prev) => [...prev, ...links])
      setChannelKey('')
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
        <select
          id={`channel-${campaignId}`}
          value={channelKey}
          onChange={(e) => setChannelKey(e.target.value)}
          disabled={channelLookup.loading}
        >
          <option value="">{channelLookup.loading ? 'Loading channels…' : 'Select a channel'}</option>
          {channelLookup.entries.map((entry) => (
            <option key={entry.key} value={entry.key}>
              {entry.label} ({entry.motion})
            </option>
          ))}
        </select>

        <label htmlFor={`variant-${campaignId}`}>Variant (optional)</label>
        <input
          id={`variant-${campaignId}`}
          value={variant}
          onChange={(e) => setVariant(e.target.value)}
          placeholder="a"
        />

        {selected !== undefined && (
          <p className="hint">
            {selected.motion === 'paid' ? 'Paid' : 'Organic'} — set by the channel, not chosen
            separately.
          </p>
        )}

        <button type="submit" disabled={channelKey === '' || minting}>
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

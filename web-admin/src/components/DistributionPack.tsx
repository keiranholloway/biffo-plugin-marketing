import { useState } from 'react'

import { getPack, type Pack } from '../lib/api'
import { useClipboard } from '../lib/useClipboard'
import { MissingPlacementsWarning, PackAssets, PackGuidance, PackLinks } from './PackParts'

/** The distribution pack (M5): assets, approved copy, tracked links and
 * guidance, assembled from an **approved** copy artefact — `pack_routes.py`'s
 * own "the milestone's actual deliverable" (M1–M4 only produce an approved
 * plan; nothing an operator can publish comes out of them without this).
 *
 * `missing_placements` is shown, not hidden: this plugin only renders a
 * campaign's source creative today, not per-placement crops (issue #36), and
 * a pack that quietly omitted that gap would look complete when it is not.
 */
export function DistributionPack({ campaignId }: { campaignId: string }) {
  const [pack, setPack] = useState<Pack | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { copied, copy, error: copyError } = useClipboard()

  async function load() {
    setLoading(true)
    setError(null)
    try {
      setPack(await getPack(campaignId))
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="pack" aria-label="Distribution pack">
      <button type="button" onClick={load} disabled={loading}>
        {loading ? 'Loading…' : pack === null ? 'Load pack' : 'Reload pack'}
      </button>

      {error !== null && <p className="error">{error}</p>}
      {copyError !== null && <p className="error">{copyError}</p>}

      {pack !== null && (
        <div className="pack-body">
          <MissingPlacementsWarning missingPlacements={pack.missing_placements} />

          <PackAssets assets={pack.assets} />

          <h4>Copy</h4>
          {pack.copy.length === 0 && <p className="empty">No approved copy channels.</p>}
          <ul className="copy-list">
            {pack.copy.map((c, i) => (
              <li key={`${c.channel}-${i}`}>
                <strong>{c.channel}</strong> <span className={`motion motion-${c.motion}`}>{c.motion}</span>
                <p className="headline">{c.headline}</p>
                <p>{c.body}</p>
                <p className="cta">{c.cta}</p>
              </li>
            ))}
          </ul>

          <PackLinks links={pack.links} copied={copied} onCopy={copy} />

          <PackGuidance guidance={pack.guidance} />
        </div>
      )}
    </section>
  )
}

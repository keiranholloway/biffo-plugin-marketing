import { useCallback, useEffect, useState, type FormEvent } from 'react'

import {
  MEDIA_KINDS,
  MOTIONS,
  createApi,
  type Api,
  type CampaignSummary,
  type MediaKind,
  type Motion,
  type NewCampaign,
} from './lib/api'

const defaultApi = createApi()

/** Founder-facing campaign studio: start a draft campaign. The server forces
 * `status = "draft"`; this form has no status control on purpose. */
export default function App({ api = defaultApi }: { api?: Api }) {
  const [name, setName] = useState('')
  const [brief, setBrief] = useState('')
  const [destination, setDestination] = useState('')
  const [motion, setMotion] = useState<Motion | ''>('')
  const [kinds, setKinds] = useState<MediaKind[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [created, setCreated] = useState<string | null>(null)
  const [campaigns, setCampaigns] = useState<CampaignSummary[] | null>(null)

  const refresh = useCallback(() => {
    api
      .listCampaigns()
      .then(setCampaigns)
      .catch(() => setCampaigns(null))
  }, [api])

  useEffect(() => {
    refresh()
  }, [refresh])

  function toggleKind(kind: MediaKind) {
    setKinds((cur) => (cur.includes(kind) ? cur.filter((k) => k !== kind) : [...cur, kind]))
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setError(null)
    setCreated(null)
    const body: NewCampaign = { name: name.trim() }
    if (brief.trim()) body.brief = brief.trim()
    if (destination.trim()) body.destination_url = destination.trim()
    if (kinds.length) body.media_kinds = kinds
    if (motion) body.motion = motion
    try {
      const result = await api.createCampaign(body)
      setCreated(result.name)
      setName('')
      setBrief('')
      setDestination('')
      setMotion('')
      setKinds([])
      refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not create the campaign.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <main>
      <h1>Marketing — campaign studio</h1>
      <h2>Start a campaign</h2>
      <p className="hint">
        New campaigns are saved as drafts. Your operator takes it from there.
      </p>
      <form onSubmit={onSubmit}>
        <label>
          Campaign name
          <input
            required
            maxLength={200}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <label>
          Brief
          <span className="hint">What you want, for whom, by when.</span>
          <textarea rows={4} value={brief} onChange={(e) => setBrief(e.target.value)} />
        </label>
        <label>
          Destination URL
          <input
            type="url"
            maxLength={1024}
            placeholder="https://"
            value={destination}
            onChange={(e) => setDestination(e.target.value)}
          />
        </label>
        <label>
          Motion
          <select value={motion} onChange={(e) => setMotion(e.target.value as Motion | '')}>
            <option value="">Decide later</option>
            {MOTIONS.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </label>
        <fieldset>
          <legend>Media</legend>
          {MEDIA_KINDS.map((k) => (
            <label key={k}>
              <span>
                <input type="checkbox" checked={kinds.includes(k)} onChange={() => toggleKind(k)} />{' '}
                {k}
              </span>
            </label>
          ))}
        </fieldset>
        {error && (
          <div role="alert" className="error">
            {error}
          </div>
        )}
        {created && (
          <div role="status" className="success">
            Draft campaign “{created}” created.
          </div>
        )}
        <button type="submit" disabled={busy || !name.trim()}>
          {busy ? 'Creating…' : 'Create draft campaign'}
        </button>
      </form>
      {campaigns && campaigns.length > 0 && (
        <section>
          <h2>Ready to promote</h2>
          <ul className="campaigns">
            {campaigns.map((c) => (
              <li key={c.id}>
                {c.name} <span className="hint">({c.status})</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </main>
  )
}

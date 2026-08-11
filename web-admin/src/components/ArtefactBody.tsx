import type { ReactNode } from 'react'

import type {
  ChannelPlanBody as ChannelPlanBodyT,
  CopySetBody,
  PositioningBody as PositioningBodyT,
  ResearchSynthesisBody,
  Source,
} from '../lib/api'
import type { ChannelLookup } from '../lib/useChannelTaxonomy'
import { ChannelName } from './ChannelName'

/** Every artefact kind's body is grounded in citations, and the citations are
 * the whole reason a human approval gate exists here at all — see
 * `pipeline.py`'s module docstring on the zero-citation guard. Shown next to
 * every claim, never collapsed into a count. */
export function SourceList({ sources }: { sources: Source[] }) {
  if (sources.length === 0) {
    return <p className="no-sources">No sources cited.</p>
  }
  return (
    <ul className="sources">
      {sources.map((s) => (
        <li key={s.url}>
          <a href={s.url} target="_blank" rel="noreferrer">
            {s.url}
          </a>
          <span className="note">{s.note}</span>
        </li>
      ))}
    </ul>
  )
}

interface FindingItem {
  key: string
  /** The item's own heading, as a full element — callers vary the tag
   * (`<h4>`/`<h5>`/`<p className="pillar">`) and content (plain text, or
   * text plus a motion badge), so this is a rendered node, not a string. */
  heading: ReactNode
  body: ReactNode
  sources: Source[]
}

/** One list of cited items — a research finding, a positioning segment,
 * pillar or CTA, a channel-plan recommendation, or one channel's copy. Every
 * artefact kind's body is a list of exactly this shape (a heading, some
 * prose, and its sources), so the four renderers below share this one loop
 * rather than repeating `.map(...) => <div className="finding">…` four
 * times with nothing to keep them in step. */
function FindingGroup({ items }: { items: FindingItem[] }) {
  return (
    <>
      {items.map((item) => (
        <div className="finding" key={item.key}>
          {item.heading}
          {item.body}
          <SourceList sources={item.sources} />
        </div>
      ))}
    </>
  )
}

export function ResearchArtefact({ body }: { body: ResearchSynthesisBody }) {
  return (
    <div className="artefact-body">
      <p className="summary">{body.summary}</p>
      <FindingGroup
        items={body.findings.map((f, i) => ({
          key: `${f.signal}-${i}`,
          heading: <h4>{f.signal}</h4>,
          body: <p>{f.why_it_matters}</p>,
          sources: f.sources,
        }))}
      />
    </div>
  )
}

export function PositioningArtefact({ body }: { body: PositioningBodyT }) {
  return (
    <div className="artefact-body">
      <h4>Segments</h4>
      <FindingGroup
        items={body.segments.map((s, i) => ({
          key: `${s.name}-${i}`,
          heading: <h5>{s.name}</h5>,
          body: <p>{s.description}</p>,
          sources: s.sources,
        }))}
      />

      <h4>Message pillars</h4>
      <FindingGroup
        items={body.pillars.map((p, i) => ({
          key: `${p.pillar}-${i}`,
          heading: <p className="pillar">{p.pillar}</p>,
          body: <p>{p.rationale}</p>,
          sources: p.sources,
        }))}
      />

      <h4>Calls to action</h4>
      <FindingGroup
        items={body.ctas.map((c, i) => ({
          key: `${c.text}-${i}`,
          heading: <p className="pillar">{c.text}</p>,
          body: <p>{c.rationale}</p>,
          sources: c.sources,
        }))}
      />
    </div>
  )
}

export function ChannelPlanArtefact({
  body,
  channelLookup,
}: {
  body: ChannelPlanBodyT
  channelLookup: ChannelLookup
}) {
  const byRank = [...body.channels].sort((a, b) => a.rank - b.rank)
  return (
    <div className="artefact-body">
      <FindingGroup
        items={byRank.map((c, i) => ({
          key: `${c.channel_key ?? c.suggested_label ?? 'proposal'}-${i}`,
          heading: (
            <h4>
              #{c.rank}{' '}
              <ChannelName channelKey={c.channel_key} suggestedLabel={c.suggested_label} lookup={channelLookup} />{' '}
              <span className={`motion motion-${c.motion}`}>{c.motion}</span>
            </h4>
          ),
          body: <p>{c.rationale}</p>,
          sources: c.sources,
        }))}
      />
    </div>
  )
}

export function CopyArtefact({ body, channelLookup }: { body: CopySetBody; channelLookup: ChannelLookup }) {
  return (
    <div className="artefact-body">
      <FindingGroup
        items={body.channels.map((c, i) => ({
          key: `${c.channel_key}-${i}`,
          heading: (
            <h4>
              <ChannelName channelKey={c.channel_key} suggestedLabel={null} lookup={channelLookup} />{' '}
              <span className={`motion motion-${c.motion}`}>{c.motion}</span>
            </h4>
          ),
          body: (
            <>
              <p className="headline">{c.headline}</p>
              <p>{c.body}</p>
              <p className="cta">{c.cta}</p>
            </>
          ),
          sources: c.sources,
        }))}
      />
    </div>
  )
}

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
 * `pipeline.py`'s module docstring on the zero-citation guard.
 *
 * This is the ONE place a source's full `url` and `note` are ever printed —
 * `ResearchArtefact` below, once per unique URL. Every downstream artefact
 * (positioning, channel plan, copy) uses `SourceRefs` instead, which points
 * back here rather than repeating the same URL and note on every segment,
 * pillar, CTA, channel and piece of copy that draws on it — the literal
 * defect reported: the same handful of research links and explanatory notes
 * "duplicated everywhere" in the campaign studio. Provenance still lives in
 * the data on every one of those items (`sources`, `Field(min_length=1)` in
 * `definitions.py`) — only the RENDERING stops repeating it in full. */
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
          {s.note !== '' && <span className="note">{s.note}</span>}
        </li>
      ))}
    </ul>
  )
}

/** The first occurrence of each URL, in encounter order — used both to build
 * the research artefact's single canonical source list (so a URL cited by
 * four findings is printed once, not four times) and to collapse one item's
 * own `sources` down to what it actually adds. */
function dedupeByUrl(sources: Source[]): Source[] {
  const seen = new Set<string>()
  const unique: Source[] = []
  for (const s of sources) {
    if (seen.has(s.url)) continue
    seen.add(s.url)
    unique.push(s)
  }
  return unique
}

/** A downstream item's compact pointer back to its evidence — a count plus,
 * on request, the bare URLs (never the note: the note is where the verbatim
 * duplication was worst, since the agent copied it wholesale from the
 * research finding it drew from). Collapsed by default via `<details>`, so
 * reviewing ten segments does not mean scrolling past the same citation ten
 * times — the count alone already answers "is this grounded", which is what
 * an operator scanning the artefact needs; the URLs are here for whoever
 * wants to check one. */
export function SourceRefs({ sources }: { sources: Source[] }) {
  const unique = dedupeByUrl(sources)
  if (unique.length === 0) {
    return <p className="no-sources">No sources cited.</p>
  }
  return (
    <details className="source-refs">
      <summary>
        {unique.length} source{unique.length === 1 ? '' : 's'} cited — see Research for the full citation
      </summary>
      <ul className="source-refs-list">
        {unique.map((s) => (
          <li key={s.url}>
            <a href={s.url} target="_blank" rel="noreferrer">
              {s.url}
            </a>
          </li>
        ))}
      </ul>
    </details>
  )
}

/** A research finding's compact pointer into the artefact's own "Sources
 * cited" list, built once from every finding's sources combined
 * (`ResearchArtefact`'s `indexByUrl`). This is what stops the WITHIN-research
 * duplication: five findings that between them cite four unique URLs used to
 * print each URL and its note up to five times over; each finding now prints
 * a numbered marker instead, and the URL/note appear exactly once, in the
 * canonical list below. The marker still links straight to the source and
 * carries its note as a `title` tooltip, so nothing is less reachable — only
 * less repeated. */
function CitationMarkers({
  sources,
  indexByUrl,
}: {
  sources: Source[]
  indexByUrl: Map<string, number>
}) {
  const unique = dedupeByUrl(sources)
  if (unique.length === 0) {
    return <p className="no-sources">No sources cited.</p>
  }
  return (
    <p className="citation-markers">
      {unique.map((s) => (
        <a
          key={s.url}
          className="citation-marker"
          href={s.url}
          target="_blank"
          rel="noreferrer"
          title={s.note !== '' ? s.note : s.url}
        >
          [{indexByUrl.get(s.url)}]
        </a>
      ))}
    </p>
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
 * times with nothing to keep them in step.
 *
 * `renderSources` is the one thing that varies: the research artefact prints
 * full citations (`SourceList`) since it is the single home for them; every
 * other artefact prints a compact reference (`SourceRefs`) instead. */
function FindingGroup({
  items,
  renderSources,
}: {
  items: FindingItem[]
  renderSources: (sources: Source[]) => ReactNode
}) {
  return (
    <>
      {items.map((item) => (
        <div className="finding" key={item.key}>
          {item.heading}
          {item.body}
          {renderSources(item.sources)}
        </div>
      ))}
    </>
  )
}

/** `summary` is deliberately "reconciled into a few paragraphs"
 * (`ResearchSynthesis.summary`'s own field description, `definitions.py`) —
 * the model already separates its topline claim from the supporting detail
 * with blank lines. Rendering the whole string as one bold `<p>` discarded
 * both signals at once: no paragraph breaks (a wall of text regardless of
 * length) and no distinction between the claim and its elaboration (all of
 * it the same heavy weight). This restores what the string already carries
 * rather than inventing new structure. A summary with no blank line in it
 * (the common case in tests, and a legitimately short real one) still comes
 * back as a single one-element array, so it renders exactly as before. */
function summaryParagraphs(summary: string): string[] {
  return summary
    .split(/\n\s*\n/)
    .map((p) => p.trim())
    .filter((p) => p !== '')
}

export function ResearchArtefact({ body }: { body: ResearchSynthesisBody }) {
  // One canonical, deduplicated citation list for the whole artefact — a URL
  // cited by several findings (common: two findings from different angles
  // independently landing on the same source) used to print its full URL and
  // note once per finding. Each finding now points at this list instead of
  // repeating it.
  const allSources = dedupeByUrl(body.findings.flatMap((f) => f.sources))
  const indexByUrl = new Map(allSources.map((s, i) => [s.url, i + 1]))
  const paragraphs = summaryParagraphs(body.summary)
  return (
    <div className="artefact-body">
      {paragraphs.map((paragraph, i) => (
        // The first paragraph is the claim (kept bold, `.summary`'s original
        // weight); anything after it is elaboration and renders regular.
        <p key={i} className={i === 0 ? 'summary' : 'summary-elaboration'}>
          {paragraph}
        </p>
      ))}
      <FindingGroup
        items={body.findings.map((f, i) => ({
          key: `${f.signal}-${i}`,
          heading: <h4>{f.signal}</h4>,
          body: <p>{f.why_it_matters}</p>,
          sources: f.sources,
        }))}
        renderSources={(sources) => <CitationMarkers sources={sources} indexByUrl={indexByUrl} />}
      />
      <h4>Sources cited</h4>
      <SourceList sources={allSources} />
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
        renderSources={(sources) => <SourceRefs sources={sources} />}
      />

      <h4>Message pillars</h4>
      <FindingGroup
        items={body.pillars.map((p, i) => ({
          key: `${p.pillar}-${i}`,
          heading: <p className="pillar">{p.pillar}</p>,
          body: <p>{p.rationale}</p>,
          sources: p.sources,
        }))}
        renderSources={(sources) => <SourceRefs sources={sources} />}
      />

      <h4>Calls to action</h4>
      <FindingGroup
        items={body.ctas.map((c, i) => ({
          key: `${c.text}-${i}`,
          heading: <p className="pillar">{c.text}</p>,
          body: <p>{c.rationale}</p>,
          sources: c.sources,
        }))}
        renderSources={(sources) => <SourceRefs sources={sources} />}
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
        renderSources={(sources) => <SourceRefs sources={sources} />}
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
        renderSources={(sources) => <SourceRefs sources={sources} />}
      />
    </div>
  )
}

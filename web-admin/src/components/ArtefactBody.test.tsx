import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { ChannelTaxonomyEntry } from '../lib/api'
import type { ChannelLookup } from '../lib/useChannelTaxonomy'
import {
  ChannelPlanArtefact,
  CopyArtefact,
  PositioningArtefact,
  ResearchArtefact,
  SourceList,
} from './ArtefactBody'

/** A stub taxonomy lookup — the real one is `useChannelTaxonomy`'s hook,
 * fetched once at the campaign-detail level; these tests only need the
 * shape it produces. */
function makeLookup(entries: ChannelTaxonomyEntry[], loading = false): ChannelLookup {
  const byKey = new Map(entries.map((e) => [e.key, e]))
  return { get: (key) => byKey.get(key), entries, loading }
}

describe('SourceList', () => {
  it('shows every source', () => {
    render(
      <SourceList
        sources={[
          { url: 'https://example.com/a', note: 'First source' },
          { url: 'https://example.com/b', note: 'Second source' },
        ]}
      />,
    )
    expect(screen.getByText('https://example.com/a')).toBeInTheDocument()
    expect(screen.getByText('First source')).toBeInTheDocument()
    expect(screen.getByText('https://example.com/b')).toBeInTheDocument()
  })

  it('says plainly when there are no sources — the zero-citation case', () => {
    // pipeline.py's own guard means this case should be rare in practice,
    // but a renderer must not crash or go blank on it.
    render(<SourceList sources={[]} />)
    expect(screen.getByText(/no sources cited/i)).toBeInTheDocument()
  })
})

describe('ResearchArtefact', () => {
  it('renders the summary and every finding with its sources', () => {
    render(
      <ResearchArtefact
        body={{
          summary: 'Owners want faster onboarding.',
          findings: [
            {
              signal: 'Competitors take 3 weeks',
              why_it_matters: 'Speed is a wedge',
              sources: [{ url: 'https://example.com/report', note: 'Industry report' }],
            },
          ],
        }}
      />,
    )
    expect(screen.getByText('Owners want faster onboarding.')).toBeInTheDocument()
    expect(screen.getByText('Competitors take 3 weeks')).toBeInTheDocument()
    expect(screen.getByText('https://example.com/report')).toBeInTheDocument()
  })
})

describe('PositioningArtefact', () => {
  it('renders segments, pillars and CTAs, each with their own sources', () => {
    render(
      <PositioningArtefact
        body={{
          segments: [
            {
              name: 'Busy owners',
              description: 'Time-poor franchise owners.',
              sources: [{ url: 'https://example.com/segment', note: 'Segment evidence' }],
            },
          ],
          pillars: [
            {
              pillar: 'Onboard in a day, not a month',
              rationale: 'Speed is the differentiator',
              sources: [{ url: 'https://example.com/pillar', note: 'Pillar evidence' }],
            },
          ],
          ctas: [
            {
              text: 'Book a demo',
              rationale: 'Lowest-friction next step',
              sources: [{ url: 'https://example.com/cta', note: 'CTA evidence' }],
            },
          ],
        }}
      />,
    )
    expect(screen.getByText('Busy owners')).toBeInTheDocument()
    expect(screen.getByText('Onboard in a day, not a month')).toBeInTheDocument()
    expect(screen.getByText('Book a demo')).toBeInTheDocument()
    expect(screen.getByText('https://example.com/segment')).toBeInTheDocument()
    expect(screen.getByText('https://example.com/pillar')).toBeInTheDocument()
    expect(screen.getByText('https://example.com/cta')).toBeInTheDocument()
  })
})

const LINKEDIN: ChannelTaxonomyEntry = {
  key: 'linkedin_organic',
  label: 'LinkedIn — organic',
  motion: 'organic',
  category: 'social',
  ad_platform: null,
}
const GOOGLE: ChannelTaxonomyEntry = {
  key: 'google_search_paid',
  label: 'Google Search ads',
  motion: 'paid',
  category: 'search',
  ad_platform: 'google',
}

describe('ChannelPlanArtefact', () => {
  it('sorts channels by rank regardless of input order, and shows the taxonomy label not the key', () => {
    render(
      <ChannelPlanArtefact
        body={{
          channels: [
            {
              channel_key: 'google_search_paid',
              suggested_label: null,
              motion: 'paid',
              rank: 2,
              rationale: 'Second priority',
              sources: [{ url: 'https://example.com/google', note: 'Search intent data' }],
            },
            {
              channel_key: 'linkedin_organic',
              suggested_label: null,
              motion: 'organic',
              rank: 1,
              rationale: 'Top priority',
              sources: [{ url: 'https://example.com/linkedin', note: 'B2B reach data' }],
            },
          ],
        }}
        channelLookup={makeLookup([LINKEDIN, GOOGLE])}
      />,
    )
    const headings = screen.getAllByRole('heading', { level: 4 }).map((h) => h.textContent)
    expect(headings[0]).toContain('LinkedIn — organic')
    expect(headings[1]).toContain('Google Search ads')
    // The raw machine key must never be what the operator reads.
    expect(screen.queryByText(/linkedin_organic/)).not.toBeInTheDocument()
    expect(screen.queryByText(/google_search_paid/)).not.toBeInTheDocument()
  })

  it('marks a channel_key with no taxonomy row as unrecognised, not blank and not the bare key as a label', () => {
    render(
      <ChannelPlanArtefact
        body={{
          channels: [
            {
              channel_key: 'deleted_channel',
              suggested_label: null,
              motion: 'organic',
              rank: 1,
              rationale: 'Grounded in evidence',
              sources: [{ url: 'https://example.com/x', note: 'n' }],
            },
          ],
        }}
        channelLookup={makeLookup([LINKEDIN, GOOGLE])}
      />,
    )
    // "deleted_channel" and "unrecognised channel" are sibling text/element
    // nodes inside the same heading, not one isolated node's full text — read
    // the heading's whole textContent rather than `getByText` on a fragment.
    const heading = screen.getAllByRole('heading', { level: 4 })[0]
    expect(heading.textContent).toContain('deleted_channel')
    expect(heading.textContent).toMatch(/unrecognised channel/i)
  })

  it('does not flash a blank channel name while the taxonomy is still loading', () => {
    render(
      <ChannelPlanArtefact
        body={{
          channels: [
            {
              channel_key: 'linkedin_organic',
              suggested_label: null,
              motion: 'organic',
              rank: 1,
              rationale: 'Grounded in evidence',
              sources: [{ url: 'https://example.com/x', note: 'n' }],
            },
          ],
        }}
        channelLookup={makeLookup([], true)}
      />,
    )
    const heading = screen.getAllByRole('heading', { level: 4 })[0]
    expect(heading.textContent?.trim()).not.toBe('')
    expect(heading.textContent).toMatch(/loading channel/i)
    // Not yet resolved, so it must not (even transiently) claim "unrecognised".
    expect(screen.queryByText(/unrecognised channel/i)).not.toBeInTheDocument()
  })

  it('renders an outside-selection proposal as structurally distinct from an approved channel (#67)', () => {
    render(
      <ChannelPlanArtefact
        body={{
          channels: [
            {
              channel_key: null,
              suggested_label: 'Reddit r/franchise',
              motion: 'organic',
              rank: 1,
              rationale: 'Strong organic conversion signal outside the selected shortlist',
              sources: [{ url: 'https://example.com/reddit', note: 'Community evidence' }],
            },
          ],
        }}
        channelLookup={makeLookup([LINKEDIN, GOOGLE])}
      />,
    )
    const heading = screen.getAllByRole('heading', { level: 4 })[0]
    expect(heading.textContent).toContain('Reddit r/franchise')
    expect(heading.textContent).toMatch(/proposed/i)
    // A proposal must never be marked "unrecognised" — that badge means "a
    // channel_key with no taxonomy row", a different failure entirely.
    expect(heading.textContent).not.toMatch(/unrecognised channel/i)
  })
})

describe('CopyArtefact', () => {
  it('renders headline, body and CTA for every channel, labelled from the taxonomy', () => {
    render(
      <CopyArtefact
        body={{
          channels: [
            {
              channel_key: 'linkedin_organic',
              motion: 'organic',
              headline: 'Faster onboarding, starting today',
              body: 'Get your team live in under a day.',
              cta: 'Book a demo',
              sources: [{ url: 'https://example.com/copy', note: 'Copy grounding' }],
            },
          ],
        }}
        channelLookup={makeLookup([LINKEDIN, GOOGLE])}
      />,
    )
    expect(screen.getByText('LinkedIn — organic')).toBeInTheDocument()
    expect(screen.getByText('Faster onboarding, starting today')).toBeInTheDocument()
    expect(screen.getByText('Get your team live in under a day.')).toBeInTheDocument()
    expect(screen.getByText('Book a demo')).toBeInTheDocument()
  })
})

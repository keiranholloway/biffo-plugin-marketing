import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import {
  ChannelPlanArtefact,
  CopyArtefact,
  PositioningArtefact,
  ResearchArtefact,
  SourceList,
} from './ArtefactBody'

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

describe('ChannelPlanArtefact', () => {
  it('sorts channels by rank regardless of input order', () => {
    render(
      <ChannelPlanArtefact
        body={{
          channels: [
            {
              channel: 'Google Search',
              motion: 'paid',
              rank: 2,
              rationale: 'Second priority',
              sources: [{ url: 'https://example.com/google', note: 'Search intent data' }],
            },
            {
              channel: 'LinkedIn',
              motion: 'organic',
              rank: 1,
              rationale: 'Top priority',
              sources: [{ url: 'https://example.com/linkedin', note: 'B2B reach data' }],
            },
          ],
        }}
      />,
    )
    const headings = screen.getAllByRole('heading', { level: 4 }).map((h) => h.textContent)
    expect(headings[0]).toContain('LinkedIn')
    expect(headings[1]).toContain('Google Search')
  })
})

describe('CopyArtefact', () => {
  it('renders headline, body and CTA for every channel', () => {
    render(
      <CopyArtefact
        body={{
          channels: [
            {
              channel: 'linkedin',
              motion: 'organic',
              headline: 'Faster onboarding, starting today',
              body: 'Get your team live in under a day.',
              cta: 'Book a demo',
              sources: [{ url: 'https://example.com/copy', note: 'Copy grounding' }],
            },
          ],
        }}
      />,
    )
    expect(screen.getByText('Faster onboarding, starting today')).toBeInTheDocument()
    expect(screen.getByText('Get your team live in under a day.')).toBeInTheDocument()
    expect(screen.getByText('Book a demo')).toBeInTheDocument()
  })
})

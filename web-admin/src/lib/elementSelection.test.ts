import { describe, expect, it } from 'vitest'

import type { Artefact } from './api'
import { elementsOf } from './elementSelection'

function artefact(body: unknown): Artefact {
  return {
    id: 'a1',
    campaign_id: 'c1',
    kind: 'research',
    status: 'proposed',
    body: JSON.stringify(body),
    citations: null,
    causation_id: null,
    agent_run_id: null,
  }
}

describe('elementsOf', () => {
  it('returns nothing for no artefact, no body, or an unparseable body', () => {
    expect(elementsOf(null)).toEqual([])
    expect(elementsOf({ ...artefact({}), body: null })).toEqual([])
    expect(elementsOf({ ...artefact({}), body: 'not json' })).toEqual([])
  })

  it('collects every findings id and its signal as the label (research)', () => {
    const result = elementsOf(
      artefact({
        summary: 'x',
        findings: [
          { id: 'f1', signal: 'Owners want faster onboarding', why_it_matters: 'x', sources: [] },
          { id: 'f2', signal: 'Competitors take 3 weeks', why_it_matters: 'x', sources: [] },
        ],
      }),
    )
    expect(result).toEqual([
      { id: 'f1', label: 'Owners want faster onboarding' },
      { id: 'f2', label: 'Competitors take 3 weeks' },
    ])
  })

  it('collects segments, pillars and ctas together (positioning)', () => {
    const result = elementsOf(
      artefact({
        segments: [{ id: 's1', name: 'Busy owners', description: 'x', sources: [] }],
        pillars: [{ id: 'p1', pillar: 'Onboard in a day', rationale: 'x', sources: [] }],
        ctas: [{ id: 'c1', text: 'Book a demo', rationale: 'x', sources: [] }],
      }),
    )
    expect(result).toEqual([
      { id: 's1', label: 'Busy owners' },
      { id: 'p1', label: 'Onboard in a day' },
      { id: 'c1', label: 'Book a demo' },
    ])
  })

  it('labels a channel-plan recommendation from its channel_key or suggested_label (#67)', () => {
    const result = elementsOf(
      artefact({
        channels: [
          {
            id: 'ch1',
            channel_key: 'linkedin_organic',
            suggested_label: null,
            motion: 'organic',
            rank: 1,
            rationale: 'x',
            sources: [],
          },
          {
            id: 'ch2',
            channel_key: null,
            suggested_label: 'Reddit r/franchise',
            motion: 'organic',
            rank: 2,
            rationale: 'x',
            sources: [],
          },
        ],
      }),
    )
    expect(result).toEqual([
      { id: 'ch1', label: 'linkedin_organic' },
      { id: 'ch2', label: 'Reddit r/franchise' },
    ])
  })

  it('labels a piece of copy with its channel and headline together, distinct from a channel-plan entry', () => {
    const result = elementsOf(
      artefact({
        channels: [
          {
            id: 'cp1',
            channel_key: 'linkedin_organic',
            motion: 'organic',
            headline: 'Faster onboarding, starting today',
            body: 'x',
            cta: 'x',
            sources: [],
          },
        ],
      }),
    )
    expect(result).toEqual([{ id: 'cp1', label: 'linkedin_organic — Faster onboarding, starting today' }])
  })

  it('skips an element with no id — a legacy body from before #150', () => {
    const result = elementsOf(
      artefact({
        summary: 'x',
        findings: [
          { signal: 'No id at all', why_it_matters: 'x', sources: [] },
          { id: 'f2', signal: 'Has an id', why_it_matters: 'x', sources: [] },
        ],
      }),
    )
    expect(result).toEqual([{ id: 'f2', label: 'Has an id' }])
  })
})

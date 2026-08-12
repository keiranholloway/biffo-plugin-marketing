import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as auth from '../lib/auth'
import { Results } from './Results'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function stubSession(jwt = 'test-jwt') {
  vi.spyOn(auth, 'getCurrentSession').mockResolvedValue({
    getIdToken: () => ({ getJwtToken: () => jwt }),
  } as never)
}

/** Matches a whole metric line — label included.
 *
 * `getByText`'s default matcher sees only an element's *direct* text-node
 * children, so a `<p><strong>Leads:</strong> 3</p>` never matches
 * `/Leads: 3/` and a negative assertion written that way passes for the
 * wrong reason. Matching on `textContent` is what ties the label to the
 * figure beside it, which is the whole claim under test (issue #114). */
const line = (pattern: RegExp) => (_: string, el: Element | null) =>
  el?.tagName === 'P' && pattern.test(el.textContent ?? '')

describe('Results', () => {
  it('shows real clicks, and leads/conversions/cost as not measurable — never as zero', async () => {
    stubSession()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          campaigns: [
            {
              campaign_id: 'c1',
              campaign_name: 'Spring',
              clicks: { total: 12, paid: 5, organic: 7, unknown_channel_type: 0 },
              leads: {
                measurable: false,
                denominator: 12,
                reason: 'No route reachable from this plugin exposes this yet.',
              },
              conversions: { measurable: false, denominator: null, reason: 'no transport' },
              cost: { measurable: false, denominator: null, reason: 'no transport' },
            },
          ],
          unattributed_clicks: 0,
        }),
      }),
    )

    render(<Results campaignId="c1" />)

    expect(await screen.findByText(/12 total — 5 paid, 7 organic/i)).toBeInTheDocument()
    expect(screen.getAllByText(/not measurable/i)).toHaveLength(3)
    expect(screen.getByText(/denominator: 12/)).toBeInTheDocument()
    // Never render "0" for an unmeasurable count.
    expect(screen.queryByText(/^0$/)).not.toBeInTheDocument()
  })

  it('renders a measurable leads figure as its value, never as "not measurable"', async () => {
    stubSession()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          campaigns: [
            {
              campaign_id: 'c1',
              campaign_name: 'Spring',
              clicks: { total: 12, paid: 5, organic: 7, unknown_channel_type: 0 },
              // The shape `results_routes.py`'s `Metric` actually serialises when
              // the instance-configured leads source answered: a real number, a
              // denominator, and `reason: null` — the `null` that used to be read
              // as text and rendered as "not measurable — undefined".
              leads: { value: 3, measurable: true, denominator: 12, reason: null },
              conversions: {
                value: null,
                measurable: false,
                denominator: null,
                reason: 'demo_requests.status has no mutation path',
              },
              cost: { value: 250, measurable: true, denominator: null, reason: null },
            },
          ],
          unattributed_clicks: 0,
        }),
      }),
    )

    render(<Results campaignId="c1" />)

    // The measurable branch: the value, and its denominator alongside it.
    expect(await screen.findByText(line(/^Leads: 3 \(denominator: 12\)$/))).toBeInTheDocument()
    expect(screen.getByText(line(/^Cost: 250$/))).toBeInTheDocument()
    // The unmeasurable branch survives, honestly, with the real reason.
    expect(
      screen.getByText(line(/^Conversions: not measurable — demo_requests\.status has no mutation path$/)),
    ).toBeInTheDocument()
    // Nothing measurable is described as unmeasurable, and no `undefined` leaks
    // out of a `reason` that is `null` precisely because the metric measured.
    expect(screen.queryByText(line(/Leads: not measurable/))).not.toBeInTheDocument()
    expect(screen.queryByText(line(/Cost: not measurable/))).not.toBeInTheDocument()
    expect(screen.queryByText(line(/undefined/))).not.toBeInTheDocument()
  })

  it('renders a measurable zero as a zero, distinct from unmeasurable', async () => {
    stubSession()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          campaigns: [
            {
              campaign_id: 'c1',
              campaign_name: 'Spring',
              clicks: { total: 9, paid: 9, organic: 0, unknown_channel_type: 0 },
              leads: { value: 0, measurable: true, denominator: 9, reason: null },
              conversions: { value: null, measurable: false, denominator: null, reason: 'no signal' },
              cost: { value: null, measurable: false, denominator: null, reason: 'no cost source' },
            },
          ],
          unattributed_clicks: 0,
        }),
      }),
    )

    render(<Results campaignId="c1" />)

    expect(await screen.findByText(line(/^Leads: 0 \(denominator: 9\)$/))).toBeInTheDocument()
    expect(screen.queryByText(line(/Leads: not measurable/))).not.toBeInTheDocument()
  })

  it('states the excluded-clicks denominator whether or not the metrics are measurable', async () => {
    stubSession()
    const campaign = (leads: unknown) => ({
      campaign_id: 'c1',
      campaign_name: 'Spring',
      clicks: { total: 12, paid: 5, organic: 7, unknown_channel_type: 0 },
      leads,
      conversions: { value: null, measurable: false, denominator: null, reason: 'no signal' },
      cost: { value: null, measurable: false, denominator: null, reason: 'no cost source' },
    })

    // Measurable — the denominator must not vanish just because leads resolved.
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          campaigns: [campaign({ value: 3, measurable: true, denominator: 12, reason: null })],
          unattributed_clicks: 4,
        }),
      }),
    )
    const measurable = render(<Results campaignId="c1" />)
    expect(await screen.findByText(/4 clicks could not be attributed to any campaign/)).toBeInTheDocument()
    measurable.unmount()

    // Unmeasurable — the same standing count, on the same terms.
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          campaigns: [campaign({ value: null, measurable: false, denominator: 12, reason: 'no leads source' })],
          unattributed_clicks: 4,
        }),
      }),
    )
    render(<Results campaignId="c1" />)
    expect(await screen.findByText(/4 clicks could not be attributed to any campaign/)).toBeInTheDocument()
  })

  it('says plainly when this campaign has no results yet', async () => {
    stubSession()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, json: async () => ({ campaigns: [], unattributed_clicks: 0 }) }),
    )

    render(<Results campaignId="missing" />)

    expect(await screen.findByText(/no results yet/i)).toBeInTheDocument()
  })

  it('reports a load failure rather than rendering nothing', async () => {
    stubSession()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 500, json: async () => ({}) }))

    render(<Results campaignId="c1" />)

    expect(await screen.findByText(/could not load results/i)).toBeInTheDocument()
  })
})

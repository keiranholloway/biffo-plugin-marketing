import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as auth from '../lib/auth'
import type { ChannelLookup } from '../lib/useChannelTaxonomy'
import { Pipeline } from './Pipeline'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  // A no-op when real timers are already active — belt-and-braces so a
  // test that enables fake timers (the polling tests below) can never leak
  // them into a later test if it fails before its own cleanup runs.
  vi.useRealTimers()
})

function stubSession(jwt = 'test-jwt') {
  vi.spyOn(auth, 'getCurrentSession').mockResolvedValue({
    getIdToken: () => ({ getJwtToken: () => jwt }),
  } as never)
}

const CAMPAIGN = 'c1'

// None of these tests exercise a rendered channel — `useChannelTaxonomy`
// itself is `CampaignDetail`'s concern, not `Pipeline`'s (see
// `CampaignDetail.test.tsx` and `ArtefactBody.test.tsx` for that). An
// already-resolved empty lookup is enough to satisfy the required prop.
const EMPTY_LOOKUP: ChannelLookup = { get: () => undefined, entries: [], loading: false }

/** A `fetch` stub that answers every stage's GET as "not started yet" (404)
 * unless `artefacts` supplies a row for that kind. */
function fetchStub(artefacts: Record<string, { status: string; body?: string | null } | undefined> = {}) {
  return vi.fn((url: string, init?: RequestInit) => {
    const match = /\/artefacts\/([a-z_]+)$/.exec(url)
    if (match && (!init || init.method === undefined)) {
      const kind = match[1]
      const row = artefacts[kind]
      if (row === undefined) {
        return Promise.resolve({ ok: false, status: 404, json: async () => ({}) })
      }
      return Promise.resolve({
        ok: true,
        json: async () => ({
          id: `a-${kind}`,
          campaign_id: CAMPAIGN,
          kind,
          status: row.status,
          body: row.body ?? null,
          citations: null,
          causation_id: null,
          agent_run_id: null,
        }),
      })
    }
    return Promise.resolve({ ok: false, status: 404, json: async () => ({}) })
  })
}

describe('Pipeline', () => {
  it('renders all four stages, gating everything past research when nothing is approved', async () => {
    stubSession()
    vi.stubGlobal('fetch', fetchStub())

    render(<Pipeline campaignId={CAMPAIGN} hasBrief={true} channelLookup={EMPTY_LOOKUP} />)

    expect(await screen.findByRole('button', { name: /start research/i })).toBeEnabled()

    const positioning = screen.getByTestId('stage-positioning')
    expect(
      await within(positioning).findByRole('button', { name: /start positioning/i }),
    ).toBeDisabled()
    expect(within(positioning).getByText(/approve the research stage first/i)).toBeInTheDocument()
  })

  it('blocks research itself when the campaign has no brief', async () => {
    stubSession()
    vi.stubGlobal('fetch', fetchStub())

    render(<Pipeline campaignId={CAMPAIGN} hasBrief={false} channelLookup={EMPTY_LOOKUP} />)

    const research = await screen.findByTestId('stage-research')
    expect(within(research).getByRole('button', { name: /start research/i })).toBeDisabled()
    expect(within(research).getByText(/no brief yet/i)).toBeInTheDocument()
  })

  it('unlocks positioning once research is approved, and renders its findings', async () => {
    stubSession()
    vi.stubGlobal(
      'fetch',
      fetchStub({
        research: {
          status: 'approved',
          body: JSON.stringify({
            summary: 'The market wants faster onboarding.',
            findings: [
              {
                signal: 'Competitors take 3 weeks to onboard',
                why_it_matters: 'Speed is a wedge',
                sources: [{ url: 'https://example.com/report', note: 'Industry report' }],
              },
            ],
          }),
        },
      }),
    )

    render(<Pipeline campaignId={CAMPAIGN} hasBrief={true} channelLookup={EMPTY_LOOKUP} />)

    expect(await screen.findByText('The market wants faster onboarding.')).toBeInTheDocument()

    const positioning = screen.getByTestId('stage-positioning')
    expect(within(positioning).getByRole('button', { name: /start positioning/i })).toBeEnabled()
  })

  it('approves a proposed stage and reflects the new status', async () => {
    stubSession()
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (typeof url === 'string' && url.endsWith('/artefacts/research/approve')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            id: 'a-research',
            campaign_id: CAMPAIGN,
            kind: 'research',
            status: 'approved',
            body: JSON.stringify({ summary: 'x', findings: [] }),
            citations: null,
            causation_id: null,
            agent_run_id: null,
          }),
        })
      }
      if (typeof url === 'string' && url.endsWith('/artefacts/research') && init?.method === undefined) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            id: 'a-research',
            campaign_id: CAMPAIGN,
            kind: 'research',
            status: 'proposed',
            body: JSON.stringify({ summary: 'x', findings: [] }),
            citations: null,
            causation_id: null,
            agent_run_id: null,
          }),
        })
      }
      return Promise.resolve({ ok: false, status: 404, json: async () => ({}) })
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    render(<Pipeline campaignId={CAMPAIGN} hasBrief={true} channelLookup={EMPTY_LOOKUP} />)

    const research = await screen.findByTestId('stage-research')
    await user.click(within(research).getByRole('button', { name: /^approve$/i }))

    expect(await within(research).findByText('Approved')).toBeInTheDocument()
  })

  it("names only the copy stage's actual missing upstream approval, not both every time", async () => {
    // Positioning is already approved here — copy's blocked message must not
    // tell the operator to redo it too, only to approve channel plan.
    stubSession()
    vi.stubGlobal(
      'fetch',
      fetchStub({
        research: { status: 'approved', body: JSON.stringify({ summary: 'x', findings: [] }) },
        positioning: {
          status: 'approved',
          body: JSON.stringify({ segments: [], pillars: [], ctas: [] }),
        },
        channel_plan: { status: 'proposed', body: JSON.stringify({ channels: [] }) },
      }),
    )

    render(<Pipeline campaignId={CAMPAIGN} hasBrief={true} channelLookup={EMPTY_LOOKUP} />)

    const copy = await screen.findByTestId('stage-copy')
    expect(await within(copy).findByText(/approve the channel plan stage first/i)).toBeInTheDocument()
    expect(within(copy).queryByText(/positioning/i)).not.toBeInTheDocument()
  })

  // #144: a pending stage now updates itself instead of only responding to
  // "Check for result" — these two exercise the real wiring in `Pipeline`
  // (`usePendingPolling` itself is unit-tested in `usePendingPolling.test.ts`
  // for the timing, backoff, unmount-cleanup and ceiling behaviour these two
  // build on).
  describe('auto-updating a pending stage', () => {
    /** Every `/artefacts/research` GET after the first `answersAfterFirst`
     * poll returns `laterResponse` — models a research run that is still
     * `pending` on the page's initial load, then resolves (or fails) on a
     * subsequent poll. */
    function pollingFetchStub(laterResponse: { ok: true; body: unknown } | { ok: false; status: number; body: unknown }) {
      let calls = 0
      return vi.fn((url: string, init?: RequestInit) => {
        if (url.endsWith('/artefacts/research') && (!init || init.method === undefined)) {
          calls += 1
          if (calls === 1) {
            return Promise.resolve({
              ok: true,
              json: async () => ({
                id: 'a-research',
                campaign_id: CAMPAIGN,
                kind: 'research',
                status: 'pending',
                body: null,
                citations: null,
                causation_id: null,
                agent_run_id: null,
              }),
            })
          }
          return Promise.resolve(
            laterResponse.ok
              ? { ok: true, json: async () => laterResponse.body }
              : { ok: false, status: laterResponse.status, json: async () => laterResponse.body },
          )
        }
        return Promise.resolve({ ok: false, status: 404, json: async () => ({}) })
      })
    }

    it('picks up a finished run on its own, with no click on "Check for result"', async () => {
      stubSession()
      const fetchMock = pollingFetchStub({
        ok: true,
        body: {
          id: 'a-research',
          campaign_id: CAMPAIGN,
          kind: 'research',
          status: 'proposed',
          body: JSON.stringify({ summary: 'The market wants faster onboarding.', findings: [] }),
          citations: null,
          causation_id: null,
          agent_run_id: null,
        },
      })
      vi.stubGlobal('fetch', fetchMock)

      render(<Pipeline campaignId={CAMPAIGN} hasBrief={true} channelLookup={EMPTY_LOOKUP} />)

      const research = await screen.findByTestId('stage-research')
      expect(within(research).getByRole('button', { name: /check for result/i })).toBeInTheDocument()

      vi.useFakeTimers()
      // The fast-window cadence is 2s — nobody clicked anything.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000)
      })

      // Not `findByText` — that falls back to a real `setTimeout`-driven
      // `waitFor`, which never wakes once fake timers are active. `act`'s
      // `await` above already flushed the resolved fetch and its resulting
      // re-render, so a synchronous query is both correct and sufficient.
      expect(within(research).getByText('The market wants faster onboarding.')).toBeInTheDocument()
      expect(within(research).queryByRole('button', { name: /check for result/i })).not.toBeInTheDocument()
    })

    it('surfaces a failed run\'s own reason instead of returning the stage to startable', async () => {
      stubSession()
      const fetchMock = pollingFetchStub({
        ok: false,
        status: 502,
        body: { detail: 'The research-synthesis run did not complete successfully.' },
      })
      vi.stubGlobal('fetch', fetchMock)

      render(<Pipeline campaignId={CAMPAIGN} hasBrief={true} channelLookup={EMPTY_LOOKUP} />)

      const research = await screen.findByTestId('stage-research')
      expect(within(research).getByRole('button', { name: /check for result/i })).toBeInTheDocument()

      vi.useFakeTimers()
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000)
      })

      expect(within(research).getByText(/did not complete successfully/i)).toBeInTheDocument()
      // Still pending, not silently reset to a startable stage — the manual
      // escape hatch is exactly what is still offered.
      expect(within(research).getByRole('button', { name: /check for result/i })).toBeInTheDocument()
      expect(within(research).queryByRole('button', { name: /^start research/i })).not.toBeInTheDocument()
    })
  })
})

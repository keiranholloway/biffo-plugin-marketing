import { act, render, screen, waitFor, within } from '@testing-library/react'
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

/** One stage, once its first fetch has landed (#158).
 *
 * **`findByTestId` on its own is not enough, and the gap is a real flake.**
 * `PipelineStage` renders its `<section data-testid="stage-…">` immediately,
 * with `<p class="empty">Loading…</p>` inside it and no controls at all; the
 * badge, the buttons and the body arrive only when the artefact fetch
 * resolves. So a bare `await screen.findByTestId('stage-research')` resolves
 * against the *skeleton*, and every synchronous `within(stage).getBy…` is a
 * race against a promise — one that the stub usually, but not always, wins.
 *
 * It failed in CI on 2026-08-15 (`Pipeline.test.tsx:316`, "Unable to find an
 * accessible element with the role button and name /check for result/i", with
 * the printed DOM showing the stage holding nothing but `Loading…`) and passed
 * on the same commit locally, which is exactly what #158 reports and exactly
 * why it reads as a regression in whatever change happens to be in flight.
 *
 * Waiting for the loading line to go is the honest wait: it is the component's
 * own statement that it has nothing to show yet. Asserting on the controls
 * directly with `findByRole` would fix one call site and leave the next one
 * to be written wrong.
 */
async function findLoadedStage(kind: string): Promise<HTMLElement> {
  const stage = await screen.findByTestId(`stage-${kind}`)
  await waitFor(() => {
    expect(within(stage).queryByText('Loading…')).not.toBeInTheDocument()
  })
  return stage
}

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

    render(<Pipeline campaignId={CAMPAIGN} hasBrief={true} hasTargets={true} channelLookup={EMPTY_LOOKUP} />)

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

    render(<Pipeline campaignId={CAMPAIGN} hasBrief={false} hasTargets={true} channelLookup={EMPTY_LOOKUP} />)

    const research = await findLoadedStage('research')
    expect(within(research).getByRole('button', { name: /start research/i })).toBeDisabled()
    expect(within(research).getByText(/no brief yet/i)).toBeInTheDocument()
  })

  it('blocks the channel plan until the campaign has a motion and target channels (#67)', async () => {
    stubSession()
    vi.stubGlobal(
      'fetch',
      fetchStub({
        research: { status: 'approved', body: JSON.stringify({ summary: 's', findings: [] }) },
        positioning: {
          status: 'approved',
          body: JSON.stringify({ segments: [], pillars: [], ctas: [] }),
        },
      }),
    )

    render(<Pipeline campaignId={CAMPAIGN} hasBrief={true} hasTargets={false} channelLookup={EMPTY_LOOKUP} />)

    const plan = await findLoadedStage('channel_plan')
    // Approved positioning is no longer enough on its own — the operator's
    // own two decisions come first, and `start_channel_plan_route` 422s
    // without them, so the button must not offer to make that call.
    expect(within(plan).getByRole('button', { name: /start channel plan/i })).toBeDisabled()
    expect(within(plan).getByText(/motion and target channels/i)).toBeInTheDocument()
  })

  it('unlocks the channel plan once positioning is approved and targeting is set', async () => {
    stubSession()
    vi.stubGlobal(
      'fetch',
      fetchStub({
        research: { status: 'approved', body: JSON.stringify({ summary: 's', findings: [] }) },
        positioning: {
          status: 'approved',
          body: JSON.stringify({ segments: [], pillars: [], ctas: [] }),
        },
      }),
    )

    render(<Pipeline campaignId={CAMPAIGN} hasBrief={true} hasTargets={true} channelLookup={EMPTY_LOOKUP} />)

    const plan = await findLoadedStage('channel_plan')
    expect(within(plan).getByRole('button', { name: /start channel plan/i })).toBeEnabled()
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

    render(<Pipeline campaignId={CAMPAIGN} hasBrief={true} hasTargets={true} channelLookup={EMPTY_LOOKUP} />)

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
    render(<Pipeline campaignId={CAMPAIGN} hasBrief={true} hasTargets={true} channelLookup={EMPTY_LOOKUP} />)

    const research = await findLoadedStage('research')
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

    render(<Pipeline campaignId={CAMPAIGN} hasBrief={true} hasTargets={true} channelLookup={EMPTY_LOOKUP} />)

    const copy = await findLoadedStage('copy')
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
      // Fake timers BEFORE render — see `findLoadedStage` and the #147 test
      // below for the two halves of why (#158).
      vi.useFakeTimers()
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

      render(<Pipeline campaignId={CAMPAIGN} hasBrief={true} hasTargets={true} channelLookup={EMPTY_LOOKUP} />)

      // Let mount's fetches settle under fake timers, so the assertion below
      // runs against a loaded stage rather than racing one.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })

      const research = screen.getByTestId('stage-research')
      expect(within(research).getByRole('button', { name: /check for result/i })).toBeInTheDocument()

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
      // Fake timers BEFORE render (#158) — this is the test CI failed on.
      vi.useFakeTimers()
      stubSession()
      const fetchMock = pollingFetchStub({
        ok: false,
        status: 502,
        body: { detail: 'The research-synthesis run did not complete successfully.' },
      })
      vi.stubGlobal('fetch', fetchMock)

      render(<Pipeline campaignId={CAMPAIGN} hasBrief={true} hasTargets={true} channelLookup={EMPTY_LOOKUP} />)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })

      const research = screen.getByTestId('stage-research')
      expect(within(research).getByRole('button', { name: /check for result/i })).toBeInTheDocument()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000)
      })

      expect(within(research).getByText(/did not complete successfully/i)).toBeInTheDocument()
      // Still pending, not silently reset to a startable stage — the manual
      // escape hatch is exactly what is still offered.
      expect(within(research).getByRole('button', { name: /check for result/i })).toBeInTheDocument()
      expect(within(research).queryByRole('button', { name: /^start research/i })).not.toBeInTheDocument()
      // #159: "not startable" must not mean "not restartable". The stage keeps
      // its badge and its reason; what it gains is an explicitly-labelled
      // re-run, which is a different control from the plain "Start" this
      // deliberately still withholds.
      expect(within(research).getByRole('button', { name: /run research again/i })).toBeInTheDocument()
    })

    it('keeps the stage rendered WHILE a poll is in flight, not just after it (#147)', async () => {
      // The flicker is a TRANSIENT state: `load()` set `loading: true` when
      // the poll started and cleared it when the response landed, and
      // `PipelineStage` renders `loading` by swapping out the WHOLE stage.
      // Asserting after the response has flushed proves nothing — the stage
      // is back by then either way. So this holds the poll's response OPEN
      // and asserts mid-flight, the only moment the defect exists.
      //
      // Fake timers are installed BEFORE render, deliberately: the polling
      // interval is created by an effect during mount, so installing them
      // afterwards leaves a real-timer interval that `advanceTimersByTime`
      // cannot drive — the poll then never fires and the test passes against
      // the bug. That is exactly how an earlier version of this test was
      // vacuous.
      vi.useFakeTimers()
      stubSession()

      let releasePoll: (() => void) | null = null
      const pendingBody = {
        id: 'a-research',
        campaign_id: CAMPAIGN,
        kind: 'research',
        status: 'pending',
        body: null,
        citations: null,
        causation_id: null,
        agent_run_id: null,
      }
      // Routed by URL and method like `pollingFetchStub`: a bare call counter
      // is wrong because every stage fetches on mount, so "the second call"
      // is another stage's first load, not research's poll.
      let researchReads = 0
      const fetchMock = vi.fn((url: string, init?: RequestInit) => {
        if (url.endsWith('/artefacts/research') && (!init || init.method === undefined)) {
          researchReads += 1
          if (researchReads === 1) {
            return Promise.resolve({ ok: true, json: async () => pendingBody })
          }
          return new Promise((resolve) => {
            releasePoll = () => resolve({ ok: true, json: async () => pendingBody })
          })
        }
        return Promise.resolve({ ok: false, status: 404, json: async () => ({}) })
      })
      vi.stubGlobal('fetch', fetchMock)

      render(<Pipeline campaignId={CAMPAIGN} hasBrief={true} hasTargets={true} channelLookup={EMPTY_LOOKUP} />)

      // Let mount's fetches settle under fake timers.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })

      const research = screen.getByTestId('stage-research')
      expect(
        within(research).getByRole('button', { name: /check for result/i }),
      ).toBeInTheDocument()

      // The 2s fast-window tick. The poll fires and does NOT come back.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000)
      })

      const researchCalls = fetchMock.mock.calls.filter(
        ([u, init]) =>
          typeof u === 'string' &&
          u.endsWith('/artefacts/research') &&
          (!init || (init as RequestInit).method === undefined),
      )
      expect(researchCalls.length).toBeGreaterThan(1)

      // Scoped with `within`: other stages are legitimately first-loading.
      expect(within(research).queryByText('Loading…')).not.toBeInTheDocument()
      expect(
        within(research).getByRole('button', { name: /check for result/i }),
      ).toBeInTheDocument()

      await act(async () => {
        releasePoll?.()
      })
      vi.useRealTimers()
    })

  })

  /** Issue #159. On tabsii dev the channel-plan stage 502'd with "the
   * channel-plan run produced no submit_channel_plan tool call", and then sat
   * at `Running…` for ever: the reason was shown (#85/#86, correct) but the
   * only control left was "Check for result", which re-reads the same dead
   * run and returns the same 502 for as long as anyone keeps pressing it.
   *
   * The artefact is still `pending` because that is exactly what happened —
   * the stage never produced anything to move it on — so no amount of
   * re-reading changes it. What resolves it is starting a NEW run, and the
   * route already allows that (a fresh `pending` artefact sorts ahead of this
   * one, `admin_app._latest_artefact`). Only the UI was missing the button.
   */
  describe('a pending stage whose run has already failed (#159)', () => {
    /** Positioning approved and targeting set, so the channel-plan gate is
     * open; channel plan `pending` on first read and then answering every
     * subsequent poll with `laterResponse`. */
    function channelPlanFetchStub(laterResponse: { status: number; body: unknown }) {
      let reads = 0
      return vi.fn((url: string, init?: RequestInit) => {
        const method = init?.method ?? 'GET'
        if (url.endsWith('/artefacts/channel_plan') && method === 'GET') {
          reads += 1
          if (reads === 1) {
            return Promise.resolve({
              ok: true,
              json: async () => ({
                id: 'a-channel_plan',
                campaign_id: CAMPAIGN,
                kind: 'channel_plan',
                status: 'pending',
                body: null,
                citations: null,
                causation_id: null,
                agent_run_id: 'run-1',
              }),
            })
          }
          return Promise.resolve({
            ok: false,
            status: laterResponse.status,
            json: async () => laterResponse.body,
          })
        }
        if (url.endsWith('/artefacts/positioning') && method === 'GET') {
          return Promise.resolve({
            ok: true,
            json: async () => ({
              id: 'a-positioning',
              campaign_id: CAMPAIGN,
              kind: 'positioning',
              status: 'approved',
              body: JSON.stringify({ segments: [], pillars: [], ctas: [] }),
              citations: null,
              causation_id: null,
              agent_run_id: null,
            }),
          })
        }
        if (url.endsWith('/channel-plan') && method === 'POST') {
          return Promise.resolve({
            ok: true,
            json: async () => ({
              id: 'a-channel_plan-2',
              campaign_id: CAMPAIGN,
              kind: 'channel_plan',
              status: 'pending',
              body: null,
              citations: null,
              causation_id: null,
              agent_run_id: 'run-2',
            }),
          })
        }
        return Promise.resolve({ ok: false, status: 404, json: async () => ({}) })
      })
    }

    /** Renders with the channel-plan stage `pending`, then lets one poll land
     * on `status`/`detail`.
     *
     * Fake timers are installed BEFORE render, for the reason the #147 test
     * above spells out: the heartbeat is created by an effect, so installing
     * them afterwards leaves a real-timer interval `advanceTimersByTime`
     * cannot drive — the poll never fires and the assertions pass or fail on
     * whichever real-time race won, which is how two of these were briefly
     * green against no code at all. */
    async function renderUntilChannelPlanFails(status: number, detail: string) {
      vi.useFakeTimers()
      stubSession()
      const fetchMock = channelPlanFetchStub({ status, body: { detail } })
      vi.stubGlobal('fetch', fetchMock)

      render(<Pipeline campaignId={CAMPAIGN} hasBrief={true} hasTargets={true} channelLookup={EMPTY_LOOKUP} />)

      // Mount's own reads settle first, so the stage is `pending` and the
      // heartbeat is running before any tick is advanced.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      const stage = screen.getByTestId('stage-channel_plan')
      expect(within(stage).getByRole('button', { name: /check for result/i })).toBeInTheDocument()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000)
      })
      return { stage, fetchMock }
    }

    it('offers a re-run when the run itself failed, so the operator is not stuck', async () => {
      const { stage } = await renderUntilChannelPlanFails(
        502,
        'the channel-plan run produced no submit_channel_plan tool call',
      )

      // The reason stays visible — this does NOT revert to #86's silent
      // "startable again", which is what hid the reason in the first place.
      expect(within(stage).getByText(/produced no submit_channel_plan tool call/i)).toBeInTheDocument()
      expect(within(stage).getByRole('button', { name: /check for result/i })).toBeInTheDocument()
      expect(within(stage).getByRole('button', { name: /run channel plan again/i })).toBeEnabled()
    })

    it('actually starts a fresh run when that re-run is pressed', async () => {
      // Driven entirely off the manual button rather than the poll, so this
      // needs no fake timers to fight `userEvent` over — and it proves the
      // other half of the same route: pressing "Check for result" on a dead
      // run is what an operator does first, and it must be what reveals the
      // re-run rather than something only the background poll can surface.
      stubSession()
      const fetchMock = channelPlanFetchStub({
        status: 502,
        body: { detail: 'the channel-plan run produced no submit_channel_plan tool call' },
      })
      vi.stubGlobal('fetch', fetchMock)
      const user = userEvent.setup()

      render(<Pipeline campaignId={CAMPAIGN} hasBrief={true} hasTargets={true} channelLookup={EMPTY_LOOKUP} />)
      const stage = await findLoadedStage('channel_plan')

      await user.click(await within(stage).findByRole('button', { name: /check for result/i }))
      await user.click(await within(stage).findByRole('button', { name: /run channel plan again/i }))

      const started = fetchMock.mock.calls.filter(
        ([u, init]) =>
          typeof u === 'string' && u.endsWith('/channel-plan') && (init as RequestInit)?.method === 'POST',
      )
      expect(started).toHaveLength(1)
      // A fresh run, so the failure that belonged to the old one goes with it.
      expect(within(stage).queryByText(/produced no submit_channel_plan tool call/i)).not.toBeInTheDocument()
    })

    it('does not offer a re-run for a failure that is not the run\'s own', async () => {
      // A 403 is the operator's session, not the agent's output. Re-running
      // would bill for a second run to fix a permissions problem — and the
      // 502 is the one status that means "an agent ran, and what it produced
      // is not something retrying the REQUEST fixes" (`admin_app.
      // _pipeline_error_to_http`), which is precisely the condition a re-run
      // is the answer to.
      const { stage } = await renderUntilChannelPlanFails(403, 'Administrator access required')

      expect(within(stage).getByText(/administrator access required/i)).toBeInTheDocument()
      expect(within(stage).getByRole('button', { name: /check for result/i })).toBeInTheDocument()
      expect(within(stage).queryByRole('button', { name: /run channel plan again/i })).not.toBeInTheDocument()
    })
  })
})

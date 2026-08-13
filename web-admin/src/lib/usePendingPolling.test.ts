import { renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { POLL_CEILING_MS, usePendingPolling, type PendingPollStage } from './usePendingPolling'

/** Stubs `document.hidden` (jsdom's own default is always `false`, and it has
 * no setter) and fires the real event the hook listens for. */
function setHidden(hidden: boolean) {
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
  document.dispatchEvent(new Event('visibilitychange'))
}

beforeEach(() => {
  vi.useFakeTimers()
  setHidden(false)
})

afterEach(() => {
  vi.useRealTimers()
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => false })
})

describe('usePendingPolling', () => {
  it('polls a pending stage and leaves a non-pending one alone', () => {
    const onPoll = vi.fn()
    const stages: PendingPollStage<'research' | 'positioning'>[] = [
      { kind: 'research', pending: true },
      { kind: 'positioning', pending: false },
    ]
    renderHook(() => usePendingPolling(stages, onPoll, vi.fn()))

    vi.advanceTimersByTime(2_000)
    expect(onPoll).toHaveBeenCalledOnce()
    expect(onPoll).toHaveBeenCalledWith('research')
  })

  it('does not poll at all while nothing is pending', () => {
    const onPoll = vi.fn()
    renderHook(() => usePendingPolling([{ kind: 'research', pending: false }], onPoll, vi.fn()))

    vi.advanceTimersByTime(60_000)
    expect(onPoll).not.toHaveBeenCalled()
  })

  it('stops polling once the stage leaves pending (a terminal state)', () => {
    const onPoll = vi.fn()
    const { rerender } = renderHook(({ pending }) => usePendingPolling([{ kind: 'research', pending }], onPoll, vi.fn()), {
      initialProps: { pending: true },
    })

    vi.advanceTimersByTime(2_000)
    expect(onPoll).toHaveBeenCalledTimes(1)

    rerender({ pending: false })
    onPoll.mockClear()

    vi.advanceTimersByTime(60_000)
    expect(onPoll).not.toHaveBeenCalled()
  })

  it('clears its timer on unmount — no poll fires afterwards', () => {
    const onPoll = vi.fn()
    const { unmount } = renderHook(() => usePendingPolling([{ kind: 'research', pending: true }], onPoll, vi.fn()))

    vi.advanceTimersByTime(2_000)
    expect(onPoll).toHaveBeenCalledTimes(1)

    expect(vi.getTimerCount()).toBeGreaterThan(0)
    unmount()
    // The classic leak this hook exists to avoid: an interval that keeps
    // firing after the component using it is gone. If cleanup missed it,
    // this assertion — not just the call count below — is what would catch
    // it, because a leaked interval still shows up in the timer count even
    // if its callback happens to be a no-op afterwards.
    expect(vi.getTimerCount()).toBe(0)

    onPoll.mockClear()
    vi.advanceTimersByTime(60_000)
    expect(onPoll).not.toHaveBeenCalled()
  })

  it('backs off from a 2s cadence to an 8s cadence after the first 30s', () => {
    const onPoll = vi.fn()
    renderHook(() => usePendingPolling([{ kind: 'research', pending: true }], onPoll, vi.fn()))

    // Fast window: 30s / 2s = 15 polls.
    vi.advanceTimersByTime(30_000)
    expect(onPoll).toHaveBeenCalledTimes(15)

    onPoll.mockClear()
    // Slow window: the next 16s at an 8s cadence is 2 more polls, not 8.
    vi.advanceTimersByTime(16_000)
    expect(onPoll).toHaveBeenCalledTimes(2)
  })

  it('stops polling and reports the ceiling once a stage has been pending too long', () => {
    const onPoll = vi.fn()
    const onCeiling = vi.fn()
    renderHook(() => usePendingPolling([{ kind: 'research', pending: true }], onPoll, onCeiling))

    vi.advanceTimersByTime(POLL_CEILING_MS)
    expect(onCeiling).toHaveBeenCalledOnce()
    expect(onCeiling).toHaveBeenCalledWith('research')

    onPoll.mockClear()
    onCeiling.mockClear()
    // Past the ceiling, neither callback should fire again for the same
    // stage — it already said its piece once, not every subsequent tick.
    vi.advanceTimersByTime(60_000)
    expect(onPoll).not.toHaveBeenCalled()
    expect(onCeiling).not.toHaveBeenCalled()
  })

  it('stops polling while the tab is hidden and resumes immediately when it becomes visible', () => {
    const onPoll = vi.fn()
    renderHook(() => usePendingPolling([{ kind: 'research', pending: true }], onPoll, vi.fn()))

    setHidden(true)
    vi.advanceTimersByTime(30_000)
    expect(onPoll).not.toHaveBeenCalled()

    setHidden(false)
    // The visibility handler itself polls immediately on resume, ahead of
    // the next heartbeat tick.
    expect(onPoll).toHaveBeenCalledTimes(1)
  })
})

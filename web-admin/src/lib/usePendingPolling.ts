import { useEffect, useRef } from 'react'

/** How soon after a stage becomes `pending` we poll, and how that eases off.
 * A flat 1s poll for the full ceiling (below) would be 240 requests per
 * stage per open tab; even a flat 2s poll would be 120. Two windows keep the
 * fast cadence only where it earns its keep — the fan-out/synthesis stages
 * are measured at 2-4 minutes and everything after at 30-90s (issue #144),
 * so a result in the first 30s is common enough to poll for quickly, and
 * past that a slower cadence is the higher-odds bet. At 2s for 30s then
 * 8s beyond it: ~15 polls in the fast window, then up to ~26 more before the
 * ceiling below is hit — about 41 requests worst case, versus 240 for a flat
 * 1s poll. */
const FAST_INTERVAL_MS = 2_000
const FAST_WINDOW_MS = 30_000
const SLOW_INTERVAL_MS = 8_000

/** Mirrors `AGENT_TIMEOUT_SECONDS` in `src/marketing/definitions.py` — the
 * agent's own wall-clock budget. A stage still `pending` this long after we
 * started watching it cannot be an in-flight run that is merely slow; it is
 * one that will never report back (a wall-clock kill, a crash CloudWatch has
 * but the API never got told about). Polling past this point only spends
 * requests on a question the agent has already stopped being able to
 * answer, so we stop and say so instead. */
export const POLL_CEILING_MS = 240_000

/** The heartbeat's own granularity — small enough that the schedule above
 * lands within a second of its target, and cheap because a tick that finds
 * nothing due does no network work at all. */
const TICK_MS = 1_000

function nextDelayMs(elapsedMs: number): number {
  return elapsedMs < FAST_WINDOW_MS ? FAST_INTERVAL_MS : SLOW_INTERVAL_MS
}

export interface PendingPollStage<K extends string> {
  kind: K
  pending: boolean
}

/** Polls every currently-`pending` stage in `stages` on ONE shared heartbeat,
 * not one `setInterval`/`visibilitychange` pair per stage.
 *
 * `Pipeline` renders four stages at once, and this pipeline's own gating
 * means at most one of them is ever `pending` simultaneously in practice —
 * but nothing here assumes that. A single timer that checks every pending
 * stage's own due time on each tick behaves identically whether one stage or
 * four are pending at once, without four independent timers drifting apart,
 * quadrupling the `visibilitychange` listener count, or leaking N intervals
 * if cleanup ever missed one.
 *
 * Only reacts to *which* stages are pending (`pendingKey`), not to the
 * `stages` array's identity — the caller's per-render array would otherwise
 * restart the heartbeat, and its backoff with it, on every unrelated
 * re-render.
 */
export function usePendingPolling<K extends string>(
  stages: PendingPollStage<K>[],
  onPoll: (kind: K) => void,
  onCeiling: (kind: K) => void,
): void {
  const onPollRef = useRef(onPoll)
  const onCeilingRef = useRef(onCeiling)
  onPollRef.current = onPoll
  onCeilingRef.current = onCeiling

  // Survive across renders without forcing one: when a stage starts or stops
  // being pending, we want to remember *when* without re-rendering to record
  // it.
  const pendingSince = useRef(new Map<K, number>())
  const nextDue = useRef(new Map<K, number>())
  const ceilingHit = useRef(new Set<K>())

  const pendingKinds = stages.filter((s) => s.pending).map((s) => s.kind)
  const pendingKey = pendingKinds.slice().sort().join(',')

  useEffect(() => {
    const since = pendingSince.current
    const due = nextDue.current
    const hit = ceilingHit.current

    for (const kind of pendingKinds) {
      if (!since.has(kind)) {
        since.set(kind, Date.now())
        due.set(kind, Date.now() + FAST_INTERVAL_MS)
        hit.delete(kind)
      }
    }
    // A kind that left the pending set (terminal state, or never started)
    // stops being tracked — this is the "stop on any terminal state" half of
    // the contract, alongside the `pending: false` filter above.
    for (const kind of Array.from(since.keys())) {
      if (!pendingKinds.includes(kind)) {
        since.delete(kind)
        due.delete(kind)
        hit.delete(kind)
      }
    }

    if (pendingKinds.length === 0) return

    let hidden = document.hidden

    function tick() {
      const now = Date.now()
      for (const kind of pendingKinds) {
        const startedAt = since.get(kind)
        if (startedAt === undefined || hit.has(kind)) continue
        const elapsed = now - startedAt
        if (elapsed >= POLL_CEILING_MS) {
          hit.add(kind)
          onCeilingRef.current(kind)
          continue
        }
        // Stop when the tab is hidden — checked after the ceiling, not
        // before, so a background tab still gets told once it crosses the
        // ceiling rather than polling forever silently.
        if (hidden) continue
        const dueAt = due.get(kind) ?? 0
        if (now >= dueAt) {
          onPollRef.current(kind)
          due.set(kind, now + nextDelayMs(elapsed))
        }
      }
    }

    const interval = setInterval(tick, TICK_MS)

    function handleVisibilityChange() {
      hidden = document.hidden
      // Resume immediately rather than waiting up to one more tick — a
      // result that finished while the tab was hidden should appear the
      // moment the operator looks back, not up to a second later.
      if (!hidden) tick()
    }
    document.addEventListener('visibilitychange', handleVisibilityChange)

    return () => {
      clearInterval(interval)
      document.removeEventListener('visibilitychange', handleVisibilityChange)
    }
    // pendingKey is the only thing this effect should re-run on — see the
    // doc comment above for why `stages` itself is deliberately not here. No
    // react-hooks lint plugin is installed in this package (see `Pipeline.tsx`),
    // so there is no exhaustive-deps rule to satisfy or suppress.
  }, [pendingKey])
}

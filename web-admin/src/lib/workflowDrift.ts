/** Comparing the fan-in workflow Core actually holds against the one this
 * build declares (issue #160).
 *
 * A TypeScript port of `marketing.fan_in_workflow.config_drift`, kept
 * deliberately narrow: it decides nothing about what to seed, it only answers
 * "which keys differ". The declared side is never authored here — it arrives
 * from `GET /admin/fan-in-workflow`, so there is exactly one declaration in
 * the repo and this module holds none of it.
 *
 * Why a port rather than asking the server: the *deployed* half can only be
 * read with the operator's Cognito token, which lives in this browser and
 * nowhere the plugin's Lambda can reach. So the comparison has to happen on
 * the side that can see both documents, and that side is here.
 *
 * **Being a port is a liability, and it has already cost once.** This file
 * landed in #167. #177 then fixed the Python half to compare prompt fields as
 * the runtime stores them (issue #175 — see `asTheRuntimeStoresIt` below), and
 * nothing carried that fix across: for ten days the studio's drift table
 * reported one character of permanent, unclearable drift on every load while
 * `--check` on the same config said it was fine, with every suite green. The
 * cases in `shared/cross-language-ports.json` are now executed by BOTH halves'
 * suites (`crossLanguagePorts.test.ts` here,
 * `tests/test_marketing_cross_language_ports.py` there), so the next
 * divergence fails a test instead of an operator (issue #119).
 */

/** What Core masks a credential-bearing config field with on read
 * (`schemas/orchestration.py`'s `SECRET_SENTINEL`). Its real value cannot be
 * read back, so a key holding it is skipped rather than reported as drift —
 * calling it stale would mean permanent, unfixable red. Must stay in step
 * with `fan_in_workflow.REDACTED_SENTINEL`. */
export const REDACTED_SENTINEL = '••••••••'

/** The config keys Core does not store verbatim, because it composes them
 * through its prompt pipeline. Must stay in step with
 * `definitions.RUNTIME_PROMPT_FIELDS`; both are checked against
 * `shared/cross-language-ports.json`. */
const RUNTIME_PROMPT_FIELDS = new Set(['instructions', 'goals'])

/** One config value as it will look **on the run**, not as declared.
 *
 * A port of `marketing.definitions.as_the_runtime_stores_it`. Core writes a
 * prompt field through `prompt_parts.compose`, which strips it, so a plain
 * string prompt comes back trimmed. Any other key is returned untouched: this
 * normalises the one transformation Core actually performs, not whitespace
 * generally — a model name that gained a trailing newline is still drift.
 *
 * **Why it exists (issue #175).** The declared synthesis instructions end in a
 * newline, so a raw comparison reported drift of exactly one character — 2047
 * declared against 2046 deployed — on every single read, and the remedy it
 * offered could never clear it: re-seeding writes 2047 back and Core strips it
 * again. A permanently-red detector is worse than none, because it teaches the
 * person reading this table to scroll past the row that matters.
 *
 * Applied to BOTH sides rather than only the declared one — the deployed side
 * is already stripped and trimming is idempotent, so it is symmetric by
 * construction rather than by luck.
 */
function asTheRuntimeStoresIt(key: string, value: unknown): unknown {
  return RUNTIME_PROMPT_FIELDS.has(key) && typeof value === 'string' ? value.trim() : value
}

/** `value` serialised with object keys sorted at every depth.
 *
 * Structural comparison needs a stable string, and `JSON.stringify` preserves
 * insertion order — so two identical configs whose keys arrived in different
 * orders compared as different. Neither half chooses that order: both read
 * JSON off the wire, the deployed copy from Core's own store and the declared
 * one from `GET /admin/fan-in-workflow`. The Python half compares dicts, which
 * have never been order-sensitive, so this was drift reported in the browser
 * and nowhere else — the same "red on two identical configs" failure as #175,
 * from a different direction.
 */
function stableJson(value: unknown): string | undefined {
  return JSON.stringify(value, (_key, inner: unknown) => {
    if (inner === null || typeof inner !== 'object' || Array.isArray(inner)) return inner
    return Object.fromEntries(
      Object.entries(inner as Record<string, unknown>).sort(([a], [b]) => (a < b ? -1 : 1)),
    )
  })
}

export type ConfigDrift = {
  key: string
  /** `undefined` when the deployed copy has no such key at all. */
  deployed: unknown
  /** `undefined` when this build no longer declares the key — a leftover from
   * an older seed is drift too, in the direction nothing else reports. */
  declared: unknown
}

/** Every key whose deployed value differs from the declared one, sorted by key.
 *
 * Compares the **union** of both key sets, not just the declared ones. A key
 * the deployed copy carries and this build does not is exactly as stale as a
 * changed model, and is the case a "does the declared config appear in the
 * deployed one" check would miss.
 */
export function configDrift(
  deployed: Record<string, unknown>,
  declared: Record<string, unknown>,
): ConfigDrift[] {
  const keys = [...new Set([...Object.keys(declared), ...Object.keys(deployed)])].sort()
  const drift: ConfigDrift[] = []
  for (const key of keys) {
    const deployedValue = deployed[key]
    if (deployedValue === REDACTED_SENTINEL) continue
    const declaredValue = declared[key]
    // Structural comparison: `output_tools` is a list of nested schema objects,
    // so identity would report drift on every render and `===` on every fetch.
    // Compared as the runtime stores it, and key-order-independently — see both
    // helpers above for the two ways this reported drift on identical configs.
    if (
      stableJson(asTheRuntimeStoresIt(key, deployedValue)) !==
      stableJson(asTheRuntimeStoresIt(key, declaredValue))
    ) {
      drift.push({ key, deployed: deployedValue, declared: declaredValue })
    }
  }
  return drift
}

/** One drift value, short enough to sit in a table cell. Mirrors the script's
 * `_short`, including its length suffix — the useful thing about a 2,800-character
 * prompt differing is usually that it is a prompt, and how long. */
export function shortenValue(value: unknown): string {
  if (value === undefined) return '(absent)'
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  if (text == null) return String(value)
  return text.length > 70 ? `${text.slice(0, 70)}… (${text.length} chars)` : text
}

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
 */

/** What Core masks a credential-bearing config field with on read
 * (`schemas/orchestration.py`'s `SECRET_SENTINEL`). Its real value cannot be
 * read back, so a key holding it is skipped rather than reported as drift —
 * calling it stale would mean permanent, unfixable red. Must stay in step
 * with `fan_in_workflow.REDACTED_SENTINEL`. */
export const REDACTED_SENTINEL = '••••••••'

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
    if (JSON.stringify(deployedValue) !== JSON.stringify(declaredValue)) {
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

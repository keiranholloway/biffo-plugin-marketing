import { useCallback, useEffect, useState } from 'react'

import {
  getDeclaredFanInWorkflow,
  listCoreWorkflows,
  seedFanInWorkflow,
  type DeclaredWorkflow,
  type DeployedWorkflow,
} from '../lib/api'
import { configDrift, shortenValue, type ConfigDrift } from '../lib/workflowDrift'

/** The fan-in workflow's deployed state, and the button that fixes it (#160).
 *
 * ## Why this panel exists at all
 *
 * The research stage does not finish by itself. A workflow definition stored
 * in Core fires the synthesis agent once both research angles are terminal,
 * and its `action_config` is a **copy** of this repo's declaration, frozen at
 * the moment somebody last ran `scripts/seed_fan_in_workflow.py`. Nothing in
 * a deploy updates it. Two days after every stage moved to the Claude 5
 * family, the deployed synthesis run was still on `opus-4.8` and a 120s clock.
 *
 * Three guards were then built around that, and all three only ever *reported*
 * it: a CI fingerprint pin, the script's `--check`, and
 * `pipeline.synthesis_config_drift`. Each names the same remedy — re-run the
 * script — and none can perform it. The remedy needs a checkout, a shell, and
 * a real Cognito admin token pasted into an environment variable, which is
 * why it kept not happening.
 *
 * This panel is the missing half: it shows the same drift **and applies it**,
 * in the session of an operator who is already authenticated as an admin. It
 * does not replace the script — an environment with no browser still has one —
 * it removes the reason the script goes unrun.
 *
 * ## Why the browser does the writing
 *
 * Core's workflow-definition routes are Cognito-admin gated and have no
 * service-principal counterpart, so the plugin's own Lambda cannot write one:
 * every outbound call it makes is SigV4-signed. The operator's ID token, held
 * here, is the only credential in the system that both halves accept. The
 * plugin serves the declaration; the browser reads what is deployed, diffs,
 * and writes.
 */
export function FanInWorkflow() {
  const [declared, setDeclared] = useState<DeclaredWorkflow | null>(null)
  const [deployed, setDeployed] = useState<DeployedWorkflow | null>(null)
  // Tri-state, not a boolean. "Core has no workflow of this name" and "Core
  // could not be read" lead to opposite actions — the first wants a seed, the
  // second must never render as one — so "not loaded yet" stays distinct from
  // "loaded, and absent" (`deployed === null` with `loaded === true`).
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [seeding, setSeeding] = useState(false)
  const [seeded, setSeeded] = useState(false)

  const load = useCallback(async () => {
    setError(null)
    setLoaded(false)
    try {
      const [declaration, workflows] = await Promise.all([
        getDeclaredFanInWorkflow(),
        listCoreWorkflows(),
      ])
      setDeclared(declaration)
      setDeployed(workflows.find((w) => w.name === declaration.name) ?? null)
      setLoaded(true)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  async function reseed() {
    if (declared == null) return
    setSeeding(true)
    setError(null)
    try {
      await seedFanInWorkflow(declared, deployed?.id ?? null)
      setSeeded(true)
      // Re-read rather than assume. What matters is what Core now holds, and
      // a panel that congratulated itself on a write it never verified would
      // be the same fail-open shape this whole issue is about.
      await load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSeeding(false)
    }
  }

  if (error != null) {
    return (
      <section className="fan-in-workflow">
        <h2>Research fan-in workflow</h2>
        <p className="error">Could not check the deployed workflow: {error}</p>
        <button type="button" onClick={() => void load()}>
          Try again
        </button>
      </section>
    )
  }

  if (!loaded || declared == null) {
    return (
      <section className="fan-in-workflow">
        <h2>Research fan-in workflow</h2>
        <p>Checking the deployed workflow…</p>
      </section>
    )
  }

  const drift: ConfigDrift[] =
    deployed?.action_config != null
      ? configDrift(deployed.action_config, declared.definition.action_config)
      : []

  return (
    <section className="fan-in-workflow">
      <h2>Research fan-in workflow</h2>
      <p className="hint">
        Research finishes only if this workflow is deployed and in step. Its configuration is a
        copy taken when it was last seeded — a deploy does not update it.
      </p>

      {deployed == null ? (
        <p className="error">
          <strong>Not seeded.</strong> No workflow named “{declared.name}” exists in Core, so a
          research run will never leave <code>pending</code> here.
        </p>
      ) : drift.length === 0 ? (
        <p className="measured">
          In step — the deployed configuration matches this build ({declared.fingerprint}).
        </p>
      ) : (
        <>
          <p className="error">
            <strong>Stale in {drift.length} key{drift.length === 1 ? '' : 's'}.</strong> The
            synthesis stage is running a configuration this build no longer declares.
          </p>
          <table>
            <thead>
              <tr>
                <th scope="col">Key</th>
                <th scope="col">Deployed</th>
                <th scope="col">This build</th>
              </tr>
            </thead>
            <tbody>
              {drift.map((entry) => (
                <tr key={entry.key}>
                  <th scope="row">{entry.key}</th>
                  <td>{shortenValue(entry.deployed)}</td>
                  <td>{shortenValue(entry.declared)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {(deployed == null || drift.length > 0) && (
        <button type="button" onClick={() => void reseed()} disabled={seeding}>
          {seeding ? 'Seeding…' : deployed == null ? 'Seed it now' : 'Re-seed it now'}
        </button>
      )}
      {seeded && drift.length === 0 && deployed != null && (
        <p className="measured">Seeded. Core now holds this build’s configuration.</p>
      )}
    </section>
  )
}

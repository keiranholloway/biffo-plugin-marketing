import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

/**
 * The Vite `base` must name THIS plugin.
 *
 * idea-scout's config was scaffolded from ideation's and kept ideation's base.
 * The built index.html then requested idea-scout's own asset filenames under
 * ideation's path — 503, blank page.
 *
 * Every other gate passed: eslint, tsc, the unit tests, and `vite build`
 * itself. `base` only affects URLs inside the emitted HTML, so nothing that
 * runs locally exercises it. It was found by loading the deployed page and
 * reading the network log.
 *
 * Deliberately NO "the config names no other plugin" assertion: idea-scout
 * records that writing one fails immediately, on the comment that explains
 * this very bug — which names the other plugin's path on purpose. A check that
 * bans a token flags the prose legitimately containing it.
 */
const ROOT = join(__dirname, '..')
const PLUGIN = 'marketing'

describe('vite base path', () => {
  const config = readFileSync(join(ROOT, 'vite.config.ts'), 'utf8')

  it('is the full API Gateway path for THIS plugin', () => {
    const match = config.match(/base:\s*'([^']+)'/)
    expect(match, 'no `base` found in vite.config.ts').not.toBeNull()
    expect(match![1]).toBe(`/api/v1/plugins/${PLUGIN}/admin/`)
  })
})

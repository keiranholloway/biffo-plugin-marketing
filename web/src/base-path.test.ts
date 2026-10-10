import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

// `base` only affects URLs inside the emitted HTML, so lint, types, tests and
// the build all pass with a wrong one. This is the check that actually looks.
const ROOT = join(__dirname, '..')

describe('vite base path', () => {
  it('is the full API Gateway /ui path for THIS plugin', () => {
    const config = readFileSync(join(ROOT, 'vite.config.ts'), 'utf8')
    const match = config.match(/base:\s*'([^']+)'/)
    expect(match, 'no `base` found in vite.config.ts').not.toBeNull()
    expect(match![1]).toBe('/api/v1/plugins/marketing/ui/')
  })
})

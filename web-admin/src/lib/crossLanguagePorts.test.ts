/** The browser half of `shared/cross-language-ports.json` (issue #119).
 *
 * Some of this plugin's logic exists twice on purpose — once here and once in
 * the Lambda — because a browser module and a Python Lambda cannot share code.
 * Before this file, each side's suite executed its **own private copy** of the
 * fixtures, so the two could disagree with everything green. Measured on
 * 2026-08-16 against `origin/dev`: changing `MAX_SLUG` below from 60 to 40 left
 * all 24 relevant Python tests and all 10 vitest tests passing, while the two
 * halves named the same download differently.
 *
 * So neither side holds an expectation of its own any more. Both read the one
 * document, and a rule changed here fails here until the Python half changes
 * with it — `tests/test_marketing_cross_language_ports.py` is its other reader,
 * and also sweeps both source trees for a pair that has no entry at all.
 *
 * The spec is read with `node:fs` rather than imported, so it can live outside
 * `web-admin/` — it belongs to neither side — without a bundler config that
 * would make it look like an asset this app ships.
 */

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import type { PackAsset } from './api'
import { assetFilename, slugify } from './assetFilename'
import { configDrift } from './workflowDrift'

type SlugCase = { why: string; input: string; expected: string }
type FilenameCase = {
  why: string
  campaign: string
  part: string
  extension: string
  expected: string
}
type DriftCase = {
  why: string
  deployed: Record<string, unknown>
  declared: Record<string, unknown>
  expected: string[]
}

type Spec = {
  ports: {
    assetFilename: {
      constants: { MAX_SLUG: number }
      slugify: SlugCase[]
      assetFilename: FilenameCase[]
    }
    workflowDrift: {
      constants: { REDACTED_SENTINEL: string; RUNTIME_PROMPT_FIELDS: string[] }
      configDrift: DriftCase[]
    }
  }
}

// `process.cwd()` is `web-admin/` for both `pnpm test` and CI's
// `working-directory: web-admin` step.
const spec = JSON.parse(
  readFileSync(resolve(process.cwd(), '../shared/cross-language-ports.json'), 'utf8'),
) as Spec

/** A `PackAsset` carrying one shared case's `part` and `extension`.
 *
 * The two implementations take deliberately different arguments — this side
 * has to infer an extension from a presigned URL's path, the Lambda is handed
 * the real one — so the case is adapted into this side's shape rather than the
 * shared document being bent to one language's signature. Everything the
 * adapter decides (`is_source`, the URL) is mechanical; the campaign, part and
 * expectation all come from the spec.
 */
function assetFor(testCase: FilenameCase): PackAsset {
  const isSource = testCase.part === 'source'
  return {
    id: 'as1',
    campaign_id: 'c1',
    media_kind: 'image',
    placement: isSource ? null : testCase.part,
    media_id: 'm1',
    is_source: isSource,
    url: `https://bucket.s3.amazonaws.com/x/9f1c.${testCase.extension}?X-Amz-Signature=abc`,
  }
}

describe('slugify, against the shared cross-language specification', () => {
  it('has cases to run', () => {
    // A parametrised suite over an empty array passes for the wrong reason,
    // which is exactly the failure this whole file is about.
    expect(spec.ports.assetFilename.slugify.length).toBeGreaterThan(4)
  })

  for (const testCase of spec.ports.assetFilename.slugify) {
    it(testCase.why, () => {
      expect(slugify(testCase.input)).toBe(testCase.expected)
    })
  }

  it('caps at exactly the length the specification states', () => {
    // The behavioural read of `MAX_SLUG`, which is not exported: the constant
    // is checked as a literal from the Python side, and its EFFECT is checked
    // here, on the code that actually runs in the browser.
    const { MAX_SLUG } = spec.ports.assetFilename.constants
    expect(slugify('a'.repeat(MAX_SLUG + 20))).toHaveLength(MAX_SLUG)
  })
})

describe('assetFilename, against the shared cross-language specification', () => {
  for (const testCase of spec.ports.assetFilename.assetFilename) {
    it(testCase.why, () => {
      expect(assetFilename(assetFor(testCase), testCase.campaign)).toBe(testCase.expected)
    })
  }
})

describe('configDrift, against the shared cross-language specification', () => {
  it('has cases to run', () => {
    expect(spec.ports.workflowDrift.configDrift.length).toBeGreaterThan(4)
  })

  for (const testCase of spec.ports.workflowDrift.configDrift) {
    it(testCase.why, () => {
      // Compared as a KEY SET: the Python half returns `{key: (deployed,
      // desired)}` and this one an array of rows, each shaped for its own
      // caller. Which keys differ is the detector's actual answer; how it is
      // rendered is each side's own business.
      const keys = configDrift(testCase.deployed, testCase.declared).map((row) => row.key)
      expect(keys).toEqual(testCase.expected)
    })
  }
})

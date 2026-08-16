/** What is deliberately NOT shared with the Lambda's half of this convention.
 *
 * The slugging rules and the `<campaign>-<part>.<ext>` composition used to be
 * duplicated here — the same cases the Python suite also kept its own copy of,
 * which is the same mistake in the tests that the two implementations make in
 * the source. Both copies were green on 2026-08-16 while the client capped
 * slugs at 40 and the Lambda at 60. That behaviour now lives once, in
 * `shared/cross-language-ports.json`, and is executed by
 * `crossLanguagePorts.test.ts` here and
 * `tests/test_marketing_cross_language_ports.py` there (issue #119).
 *
 * What stays below is this side's alone: the Lambda is handed a real extension
 * and a definite part, while the browser has to infer both from a `PackAsset`
 * and a presigned URL. There is nothing for the other half to agree with.
 */

import { describe, expect, it } from 'vitest'

import type { PackAsset } from './api'
import { assetFilename } from './assetFilename'

function asset(overrides: Partial<PackAsset> = {}): PackAsset {
  return {
    id: 'as1',
    campaign_id: 'c1',
    media_kind: 'image',
    placement: null,
    media_id: 'm1',
    is_source: true,
    url: 'https://bucket.s3.amazonaws.com/plugins/marketing/9f1c.png?X-Amz-Signature=abc',
    ...overrides,
  }
}

describe('assetFilename, on the parts only the browser has to work out', () => {
  it('never lets the storage uuid reach what the operator sees', () => {
    // The regression #122 is about: the bytes are named by a uuid and Core
    // signs that uuid into Content-Disposition, so six saved creatives arrived
    // as six indistinguishable names.
    expect(assetFilename(asset(), 'Spring Launch')).not.toContain('9f1c')
  })

  it('keeps the real extension from the URL path, ignoring the presign query', () => {
    // The query is where the whole SigV4 presign lives, so a naive last-dot
    // split reads the tail of a signature as an extension.
    const jpeg = asset({ url: 'https://bucket.s3.amazonaws.com/x/y.jpeg?X-Amz-Expires=900' })

    expect(assetFilename(jpeg, 'Spring Launch')).toBe('spring-launch-source.jpeg')
  })

  it('falls back to .png when the URL carries no usable extension', () => {
    const bare = asset({ url: 'https://bucket.s3.amazonaws.com/x/9f1c?X-Amz-Expires=900' })

    expect(assetFilename(bare, 'Spring Launch')).toBe('spring-launch-source.png')
  })

  it('falls back to .png when the URL will not parse at all', () => {
    expect(assetFilename(asset({ url: '' }), 'Spring Launch')).toBe('spring-launch-source.png')
  })

  it('names an asset that is neither source nor placed a "creative"', () => {
    // This side's own fallback, from a flag the Lambda is never given — the
    // Lambda's equivalent is `asset`, recorded as a deliberate difference in
    // the shared spec so nobody later "fixes" the two into agreement.
    expect(assetFilename(asset({ is_source: null }), 'Spring Launch')).toBe(
      'spring-launch-creative.png',
    )
  })
})

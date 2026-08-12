import { describe, expect, it } from 'vitest'

import type { PackAsset } from './api'
import { assetFilename, slugify } from './assetFilename'

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

describe('slugify', () => {
  it('lowercases, strips punctuation and collapses separators', () => {
    expect(slugify('Spring Launch — 2026!')).toBe('spring-launch-2026')
  })

  it('turns a placement key into readable path-safe words', () => {
    expect(slugify('feed_1x1')).toBe('feed-1x1')
  })

  it('is empty for a value with nothing sluggable in it', () => {
    expect(slugify('!!!')).toBe('')
    expect(slugify('   ')).toBe('')
  })

  it('caps length, and never ends on a separator after the cap', () => {
    const slug = slugify(`${'a'.repeat(58)} bbbbbbbbbb`)
    expect(slug.length).toBeLessThanOrEqual(60)
    expect(slug.endsWith('-')).toBe(false)
  })
})

describe('assetFilename', () => {
  it('names a placement render after the campaign and the placement', () => {
    expect(assetFilename(asset({ placement: 'feed_1x1', is_source: false }), 'Spring Launch')).toBe(
      'spring-launch-feed-1x1.png',
    )
  })

  it('names the source creative "source" rather than a storage key', () => {
    expect(assetFilename(asset(), 'Spring Launch')).toBe('spring-launch-source.png')
    // The storage key (a uuid) must not leak into what the operator sees.
    expect(assetFilename(asset(), 'Spring Launch')).not.toContain('9f1c')
  })

  it('keeps the real extension from the URL path, ignoring the presign query', () => {
    const jpeg = asset({ url: 'https://bucket.s3.amazonaws.com/x/y.jpeg?X-Amz-Expires=900' })
    expect(assetFilename(jpeg, 'Spring Launch')).toBe('spring-launch-source.jpeg')
  })

  it('falls back to .png when the URL carries no usable extension', () => {
    const bare = asset({ url: 'https://bucket.s3.amazonaws.com/x/9f1c?X-Amz-Expires=900' })
    expect(assetFilename(bare, 'Spring Launch')).toBe('spring-launch-source.png')
  })

  it('falls back to "campaign" when the campaign name slugs to nothing', () => {
    expect(assetFilename(asset(), '   ')).toBe('campaign-source.png')
  })

  it('falls back to "creative" for an asset that is neither source nor placed', () => {
    expect(assetFilename(asset({ is_source: null }), 'Spring Launch')).toBe(
      'spring-launch-creative.png',
    )
  })
})

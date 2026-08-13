import { describe, expect, it } from 'vitest'

import type { Campaign } from './api'
import { destinationSuggestions } from './destinationSuggestions'

function campaign(overrides: Partial<Campaign> = {}): Campaign {
  return {
    id: 'c1',
    name: 'Campaign',
    status: 'draft',
    destination_url: null,
    ...overrides,
  }
}

describe('destinationSuggestions', () => {
  it('is empty when no campaign has a destination_url', () => {
    expect(destinationSuggestions([campaign({ destination_url: null })])).toEqual([])
  })

  it('lists a single used destination', () => {
    const result = destinationSuggestions([campaign({ destination_url: 'https://x.test/a' })])
    expect(result).toEqual(['https://x.test/a'])
  })

  it('deduplicates repeated destinations to one entry', () => {
    const result = destinationSuggestions([
      campaign({ id: '1', destination_url: 'https://x.test/a' }),
      campaign({ id: '2', destination_url: 'https://x.test/a' }),
    ])
    expect(result).toEqual(['https://x.test/a'])
  })

  it('orders by how often a destination has been used, most-used first', () => {
    const result = destinationSuggestions([
      campaign({ id: '1', destination_url: 'https://x.test/once' }),
      campaign({ id: '2', destination_url: 'https://x.test/twice' }),
      campaign({ id: '3', destination_url: 'https://x.test/twice' }),
      campaign({ id: '4', destination_url: 'https://x.test/thrice' }),
      campaign({ id: '5', destination_url: 'https://x.test/thrice' }),
      campaign({ id: '6', destination_url: 'https://x.test/thrice' }),
    ])
    expect(result).toEqual(['https://x.test/thrice', 'https://x.test/twice', 'https://x.test/once'])
  })

  it('keeps first-seen order between destinations used equally often', () => {
    const result = destinationSuggestions([
      campaign({ id: '1', destination_url: 'https://x.test/first' }),
      campaign({ id: '2', destination_url: 'https://x.test/second' }),
    ])
    expect(result).toEqual(['https://x.test/first', 'https://x.test/second'])
  })

  it('ignores a null destination_url without breaking on the rest', () => {
    const result = destinationSuggestions([
      campaign({ id: '1', destination_url: null }),
      campaign({ id: '2', destination_url: 'https://x.test/a' }),
    ])
    expect(result).toEqual(['https://x.test/a'])
  })

  it('ignores a blank/whitespace-only destination_url', () => {
    const result = destinationSuggestions([
      campaign({ id: '1', destination_url: '   ' }),
      campaign({ id: '2', destination_url: 'https://x.test/a' }),
    ])
    expect(result).toEqual(['https://x.test/a'])
  })

  it('is empty for an empty campaigns array', () => {
    expect(destinationSuggestions([])).toEqual([])
  })
})

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { ChannelTaxonomyEntry } from '../lib/api'
import type { ChannelLookup } from '../lib/useChannelTaxonomy'
import { PublishLink } from './PublishLink'

function makeLookup(entries: ChannelTaxonomyEntry[], loading = false): ChannelLookup {
  const byKey = new Map(entries.map((e) => [e.key, e]))
  return { get: (key) => byKey.get(key), entries, loading }
}

const LINKEDIN_ORGANIC: ChannelTaxonomyEntry = {
  key: 'linkedin_organic',
  label: 'LinkedIn — organic',
  motion: 'organic',
  category: 'social',
  ad_platform: null,
  publish_url: 'https://www.linkedin.com/feed/?shareActive=true',
}

// `trade_press_earned` (#103's own example): a pitch to a publication, not a
// composer — the seed script leaves this one's `publish_url` unset.
const TRADE_PRESS: ChannelTaxonomyEntry = {
  key: 'trade_press_earned',
  label: 'Trade press — earned coverage',
  motion: 'organic',
  category: 'trade_press',
  ad_platform: null,
  publish_url: null,
}

describe('PublishLink', () => {
  it('renders a new-tab link to the taxonomy publish_url when one is set', () => {
    render(<PublishLink channelKey="linkedin_organic" lookup={makeLookup([LINKEDIN_ORGANIC])} />)
    const link = screen.getByRole('link', { name: /publish on linkedin — organic/i })
    expect(link).toHaveAttribute('href', 'https://www.linkedin.com/feed/?shareActive=true')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('renders nothing — not a dead or placeholder link — when the channel has no publish_url', () => {
    const { container } = render(<PublishLink channelKey="trade_press_earned" lookup={makeLookup([TRADE_PRESS])} />)
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing for a channel_key the taxonomy does not recognise', () => {
    const { container } = render(<PublishLink channelKey="unknown_channel" lookup={makeLookup([LINKEDIN_ORGANIC])} />)
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing while the taxonomy is still loading, rather than a flash of a broken link', () => {
    const { container } = render(<PublishLink channelKey="linkedin_organic" lookup={makeLookup([], true)} />)
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(container).toBeEmptyDOMElement()
  })
})

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { ChannelTaxonomyEntry } from '../lib/api'
import type { ChannelLookup } from '../lib/useChannelTaxonomy'
import { ChannelName } from './ChannelName'

const LINKEDIN_PAID: ChannelTaxonomyEntry = {
  key: 'linkedin_paid',
  label: 'LinkedIn ads',
  motion: 'paid',
  category: 'social',
  ad_platform: 'linkedin',
}

function fakeLookup(entries: ChannelTaxonomyEntry[], loading = false): ChannelLookup {
  const byKey = new Map(entries.map((e) => [e.key, e]))
  return { get: (key) => byKey.get(key), entries, loading }
}

describe('ChannelName', () => {
  it('renders the taxonomy label for a resolved key, never the raw key', () => {
    render(<ChannelName channelKey="linkedin_paid" suggestedLabel={null} lookup={fakeLookup([LINKEDIN_PAID])} />)

    expect(screen.getByText('LinkedIn ads')).toBeInTheDocument()
    expect(screen.queryByText('linkedin_paid')).not.toBeInTheDocument()
  })

  it('shows a neutral placeholder while the taxonomy is still loading, not the bare key', () => {
    render(<ChannelName channelKey="linkedin_paid" suggestedLabel={null} lookup={fakeLookup([], true)} />)

    expect(screen.getByText(/loading channel/i)).toBeInTheDocument()
    expect(screen.queryByText('linkedin_paid')).not.toBeInTheDocument()
  })

  it('marks a proposal (no channel_key yet) as outside the selection, using its suggested label', () => {
    render(<ChannelName channelKey={null} suggestedLabel="Community meetups" lookup={fakeLookup([])} />)

    expect(screen.getByText('Community meetups')).toBeInTheDocument()
    expect(screen.getByText(/outside selection/i)).toBeInTheDocument()
  })

  /** #84/#85's decision on existing free-text rows: minted before the
   * picker existed (e.g. `"totally made up channel"`, minted on dev during
   * #84's own repro), these have no taxonomy row and are NOT migrated or
   * deleted — they are left exactly as this test asserts: visibly marked
   * unrecognised, never silently dropped from grouping and never rendered
   * as if the raw value were a real label. Every renderer that shows a
   * channel goes through this component, so the decision is enforced here
   * once rather than at each call site. */
  it('renders a legacy free-text channel as visibly unrecognised, never blank and never as if it were a label', () => {
    render(
      <ChannelName
        channelKey="totally made up channel"
        suggestedLabel={null}
        lookup={fakeLookup([LINKEDIN_PAID])}
      />,
    )

    expect(screen.getByText('totally made up channel')).toBeInTheDocument()
    expect(screen.getByText(/unrecognised channel/i)).toBeInTheDocument()
  })
})

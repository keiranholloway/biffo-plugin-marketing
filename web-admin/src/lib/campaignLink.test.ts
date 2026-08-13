import { describe, expect, it } from 'vitest'

import { CAMPAIGN_PARAM, campaignUrl, readCampaignParam } from './campaignLink'

const ID = '5380e9cf-457f-4932-a7ff-c1c6986524e5'
const PATH = '/api/v1/plugins/marketing/admin'

describe('readCampaignParam', () => {
  it('reads the id a shared link is asking for', () => {
    expect(readCampaignParam(`?${CAMPAIGN_PARAM}=${ID}`)).toBe(ID)
  })

  it('is null when the URL asks for no campaign', () => {
    expect(readCampaignParam('')).toBeNull()
    expect(readCampaignParam('?other=1')).toBeNull()
  })

  it('rejects a value that is not a campaign id rather than passing it on', () => {
    // A URL bar accepts anything. Rejecting here keeps "not an id" separate
    // from "an id you cannot see", and stops junk being echoed into history.
    for (const junk of ['nonsense', '../../etc/passwd', '<script>', '123', `${ID}-extra`]) {
      expect(readCampaignParam(`?${CAMPAIGN_PARAM}=${encodeURIComponent(junk)}`)).toBeNull()
    }
  })

  it('tolerates surrounding whitespace, which copy-paste adds', () => {
    expect(readCampaignParam(`?${CAMPAIGN_PARAM}=${encodeURIComponent(` ${ID} `)}`)).toBe(ID)
  })

  it('is case-insensitive about the uuid, since a link may be normalised', () => {
    expect(readCampaignParam(`?${CAMPAIGN_PARAM}=${ID.toUpperCase()}`)).toBe(ID.toUpperCase())
  })
})

describe('campaignUrl', () => {
  it('addresses an open campaign without touching the path', () => {
    // The path is load-bearing: the shell answers on `…/admin` and a trailing
    // slash 401s, so this function must never rewrite it.
    expect(campaignUrl(ID, PATH, '')).toBe(`${PATH}?${CAMPAIGN_PARAM}=${ID}`)
  })

  it('drops the parameter when the campaign is closed', () => {
    // A stale `?campaign=` on the list view would make the address bar
    // disagree with the screen.
    expect(campaignUrl(null, PATH, `?${CAMPAIGN_PARAM}=${ID}`)).toBe(PATH)
  })

  it('preserves other parameters it does not own', () => {
    const out = campaignUrl(ID, PATH, '?debug=1')
    expect(out).toContain('debug=1')
    expect(out).toContain(`${CAMPAIGN_PARAM}=${ID}`)
  })

  it('replaces rather than appends when one is already present', () => {
    const other = '11111111-2222-3333-4444-5555aaaabbbb'
    const out = campaignUrl(other, PATH, `?${CAMPAIGN_PARAM}=${ID}`)
    expect(out).toBe(`${PATH}?${CAMPAIGN_PARAM}=${other}`)
    expect(out).not.toContain(ID)
  })

  it('round-trips: what it writes, the reader reads back', () => {
    const written = campaignUrl(ID, PATH, '')
    const search = written.slice(written.indexOf('?'))
    expect(readCampaignParam(search)).toBe(ID)
  })
})

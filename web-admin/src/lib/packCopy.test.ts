import { describe, expect, it } from 'vitest'

import { composeChannelCopy } from './packCopy'

describe('composeChannelCopy', () => {
  it('joins headline, body, CTA and the tracked link with a blank line between each', () => {
    const text = composeChannelCopy({
      headline: 'Fast onboarding, done right',
      body: 'Get every franchise unit live in a day.',
      cta: 'Book a demo',
      linkUrl: 'https://x/c/tok',
    })
    expect(text).toBe(
      ['Fast onboarding, done right', 'Get every franchise unit live in a day.', 'Book a demo', 'https://x/c/tok'].join(
        '\n\n',
      ),
    )
  })

  it('omits the link line entirely when there is no tracked link yet, rather than a placeholder', () => {
    const text = composeChannelCopy({
      headline: 'Fast onboarding, done right',
      body: 'Get every franchise unit live in a day.',
      cta: 'Book a demo',
      linkUrl: null,
    })
    expect(text).toBe(['Fast onboarding, done right', 'Get every franchise unit live in a day.', 'Book a demo'].join('\n\n'))
    expect(text).not.toContain('null')
  })
})

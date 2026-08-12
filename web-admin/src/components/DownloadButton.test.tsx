import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { DownloadButton } from './DownloadButton'

const URL_ = 'https://bucket.s3.amazonaws.com/plugins/marketing/9f1c.png?X-Amz-Signature=abc'

describe('DownloadButton', () => {
  it('is a real link to the URL the page already holds, not a re-derived one', () => {
    render(<DownloadButton url={URL_} filename="spring-launch-source.png" />)
    expect(screen.getByRole('link', { name: 'Download' })).toHaveAttribute('href', URL_)
  })

  it('asks for a meaningful filename rather than the storage key', () => {
    render(<DownloadButton url={URL_} filename="spring-launch-source.png" />)
    expect(screen.getByRole('link', { name: 'Download' })).toHaveAttribute(
      'download',
      'spring-launch-source.png',
    )
  })

  it('opens in a new context so an expired presign cannot replace the pack', () => {
    render(<DownloadButton url={URL_} filename="spring-launch-source.png" />)
    const link = screen.getByRole('link', { name: 'Download' })
    expect(link).toHaveAttribute('target', '_blank')
    // No Referer on a presigned URL, and no window.opener handle either.
    expect(link).toHaveAttribute('rel', 'noreferrer')
  })

  it('takes an accessible name, so a grid of them is distinguishable', () => {
    render(
      <DownloadButton
        url={URL_}
        filename="spring-launch-feed-1x1.png"
        accessibleName="Download feed_1x1 creative"
      />,
    )
    expect(screen.getByRole('link', { name: 'Download feed_1x1 creative' })).toBeInTheDocument()
  })

  it('is keyboard-focusable', async () => {
    const { default: userEvent } = await import('@testing-library/user-event')
    const user = userEvent.setup()
    render(<DownloadButton url={URL_} filename="spring-launch-source.png" />)
    await user.tab()
    expect(screen.getByRole('link', { name: 'Download' })).toHaveFocus()
  })
})

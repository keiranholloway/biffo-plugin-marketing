import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { CopyButton } from './CopyButton'

describe('CopyButton', () => {
  it('calls onCopy with its own text, and shows the copied label once copied is that text', async () => {
    const user = userEvent.setup()
    const onCopy = vi.fn()

    const { rerender } = render(<CopyButton text="Book a demo" copied={null} onCopy={onCopy} />)
    const button = screen.getByRole('button', { name: 'Copy' })
    await user.click(button)
    expect(onCopy).toHaveBeenCalledWith('Book a demo')

    // The caller owns `copied` state (one shared `useClipboard()` per pack) —
    // this only renders whatever it is told.
    rerender(<CopyButton text="Book a demo" copied="Book a demo" onCopy={onCopy} />)
    expect(screen.getByRole('button', { name: 'Copied' })).toBeInTheDocument()
  })

  it('does not show "Copied" for a different button sharing the same copied state', () => {
    render(<CopyButton text="Book a demo" copied="Some other text" onCopy={() => {}} />)
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  it('accepts custom labels — used for a trimmed field and the copy-all control', () => {
    render(
      <CopyButton text="Headline" label="Copy headline (trimmed)" copiedLabel="Copied all" copied={null} onCopy={() => {}} />,
    )
    expect(screen.getByRole('button', { name: 'Copy headline (trimmed)' })).toBeInTheDocument()
  })

  it('is a real <button> — keyboard-focusable and activatable without a mouse', async () => {
    const user = userEvent.setup()
    const onCopy = vi.fn()
    render(<CopyButton text="Book a demo" copied={null} onCopy={onCopy} />)

    await user.tab()
    expect(screen.getByRole('button', { name: 'Copy' })).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(onCopy).toHaveBeenCalledWith('Book a demo')
  })
})

import { describe, expect, it } from 'vitest'

import { configDrift, REDACTED_SENTINEL, shortenValue } from './workflowDrift'

describe('configDrift', () => {
  it('reports nothing when the deployed copy matches the declaration', () => {
    const config = { model: 'anthropic/claude-opus-5', timeout_seconds: 240 }

    expect(configDrift({ ...config }, { ...config })).toEqual([])
  })

  it('reports a changed value with both sides, so an operator can see which is which', () => {
    const drift = configDrift(
      { model: 'anthropic/claude-opus-4.8' },
      { model: 'anthropic/claude-opus-5' },
    )

    expect(drift).toEqual([
      { key: 'model', deployed: 'anthropic/claude-opus-4.8', declared: 'anthropic/claude-opus-5' },
    ])
  })

  it('reports a key the deployed copy is missing entirely', () => {
    // This is the shape that produced #160's 120s clock: nothing wrote
    // `timeout_seconds`, and the runtime silently substituted its own default.
    // An absent key is drift, not agreement.
    const drift = configDrift({}, { timeout_seconds: 240 })

    expect(drift).toEqual([{ key: 'timeout_seconds', deployed: undefined, declared: 240 }])
  })

  it('reports a leftover key this build no longer declares', () => {
    const drift = configDrift({ retired_option: true }, {})

    expect(drift).toEqual([{ key: 'retired_option', deployed: true, declared: undefined }])
  })

  it('compares nested structures by value, not identity', () => {
    // `output_tools` is a list of nested JSON schemas rebuilt on every fetch,
    // so an identity comparison would report permanent drift on a config that
    // is in step — and a permanently red panel is one people stop reading.
    const tools = [{ type: 'function', function: { name: 'submit_research_synthesis' } }]

    expect(
      configDrift({ output_tools: structuredClone(tools) }, { output_tools: tools }),
    ).toEqual([])
  })

  it('skips a value Core masked as a secret rather than calling it permanent drift', () => {
    const drift = configDrift({ model: REDACTED_SENTINEL }, { model: 'anthropic/claude-opus-5' })

    expect(drift).toEqual([])
  })

  it('sorts by key so the same drift renders in the same order twice', () => {
    const drift = configDrift({}, { model: 'a', agent_name: 'b', timeout_seconds: 1 })

    expect(drift.map((d) => d.key)).toEqual(['agent_name', 'model', 'timeout_seconds'])
  })
})

describe('shortenValue', () => {
  it('renders an absent value as absent, not as the string "undefined"', () => {
    expect(shortenValue(undefined)).toBe('(absent)')
  })

  it('leaves a short value alone', () => {
    expect(shortenValue('anthropic/claude-opus-5')).toBe('anthropic/claude-opus-5')
  })

  it('truncates a long one and says how long it was', () => {
    // The useful thing about a 2,800-character prompt differing is that it is
    // a prompt and roughly how big — not its first 70 characters alone.
    const long = 'x'.repeat(200)

    expect(shortenValue(long)).toBe(`${'x'.repeat(70)}… (200 chars)`)
  })

  it('serialises a structured value rather than printing [object Object]', () => {
    expect(shortenValue({ a: 1 })).toBe('{"a":1}')
  })
})

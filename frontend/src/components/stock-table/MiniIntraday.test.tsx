// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, it, vi } from 'vitest'
import { MiniIntraday } from './MiniIntraday'

it('keeps distinct gradient IDs stable across loaded, empty and restored data', async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  const host = document.createElement('div')
  const root = createRoot(host)
  const rows = [
    { datetime: '2026-09-30 09:31', open: 10, high: 11, low: 10, close: 11, volume: 100, amount: 105000 },
    { datetime: '2026-09-30 09:32', open: 11, high: 12, low: 11, close: 12, volume: 200, amount: 230000 },
  ]
  const render = (empty = false) => act(async () => {
    root.render(<>
      <MiniIntraday rows={empty ? [] : rows} />
      <MiniIntraday rows={rows} />
    </>)
  })
  try {
    await render()
    const ids = Array.from(host.querySelectorAll('linearGradient'), node => node.id)
    expect(new Set(ids).size).toBe(2)
    await render(true)
    expect(host.querySelector('[aria-label="暂无分时"]')).not.toBeNull()
    await render()
    expect(Array.from(host.querySelectorAll('linearGradient'), node => node.id)).toEqual(ids)
    expect(Array.from(host.querySelectorAll('polygon'), node => node.getAttribute('fill')))
      .toEqual(ids.map(id => `url(#${id})`))
  } finally {
    await act(async () => root.unmount())
    vi.unstubAllGlobals()
  }
})

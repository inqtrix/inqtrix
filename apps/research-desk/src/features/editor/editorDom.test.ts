import { describe, expect, it, vi } from 'vitest'

import { applyScrollableTableSemantics } from './editorDom'

describe('applyScrollableTableSemantics', () => {
  it('makes every generated table wrapper a named keyboard region', () => {
    const wrappers = [
      { setAttribute: vi.fn(), tabIndex: -1 },
      { setAttribute: vi.fn(), tabIndex: -1 },
    ]
    const root = {
      querySelectorAll: vi.fn(() => wrappers),
    } as unknown as ParentNode

    expect(applyScrollableTableSemantics(root, 'Scrollable table')).toBe(2)
    expect(root.querySelectorAll).toHaveBeenCalledWith('.tableWrapper')
    for (const wrapper of wrappers) {
      expect(wrapper.setAttribute).toHaveBeenCalledWith('aria-label', 'Scrollable table')
      expect(wrapper.setAttribute).toHaveBeenCalledWith('role', 'region')
      expect(wrapper.tabIndex).toBe(0)
    }
  })
})

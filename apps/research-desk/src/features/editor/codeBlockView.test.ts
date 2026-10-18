import { describe, expect, it } from 'vitest'

import { nodeAttributeEditingState } from './nodeAttributeEditing'

/**
 * Node attributes can be changed only in plain edit mode. In suggest
 * mode the schema guard hard-rejects attribute-only transactions (pin
 * in packages/editor-schema/tests/schema.test.ts) — the picker must be
 * visibly disabled there instead of surfacing that error; comment mode
 * and read-only lifecycles have no write path at all.
 */
describe('nodeAttributeEditingState', () => {
  it('enables the picker only in plain edit mode', () => {
    expect(nodeAttributeEditingState('edit', true)).toEqual({
      enabled: true,
      reason: null,
    })
    expect(nodeAttributeEditingState(undefined, true)).toEqual({
      enabled: true,
      reason: null,
    })
  })

  it('disables with a mode reason in suggest AND comment mode', () => {
    expect(nodeAttributeEditingState('suggest', true)).toEqual({
      enabled: false,
      reason: 'mode',
    })
    expect(nodeAttributeEditingState('comment', true)).toEqual({
      enabled: false,
      reason: 'mode',
    })
  })

  it('read-only beats every mode', () => {
    expect(nodeAttributeEditingState('edit', false)).toEqual({
      enabled: false,
      reason: 'readonly',
    })
    expect(nodeAttributeEditingState('suggest', false)).toEqual({
      enabled: false,
      reason: 'readonly',
    })
  })
})

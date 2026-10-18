import { describe, expect, it } from 'vitest'
import { agentOverridesFromSelection } from './modelSelection'
import { catalogSelectionKind } from './modelLabels'

describe('agentOverridesFromSelection', () => {
  it('serializes an explicit normal override even when no model is selected', () => {
    expect(agentOverridesFromSelection('normal', null, null, null)).toEqual({
      depth: 'normal',
    })
  })

  it('combines deep with the existing explicit-model precedence', () => {
    expect(
      agentOverridesFromSelection('deep', 'high', 'model-x', 'high'),
    ).toEqual({ depth: 'deep', model: 'model-x', effort: 'high' })
  })
})

describe('catalogSelectionKind', () => {
  it('names a tier without an explicit model as a tier, not the server default', () => {
    // This is the whole point: a catalog picker that saw only model ids would
    // make a tier coming from the account preference look exactly like "no
    // selection", and the composer would claim the server default while the
    // request actually carried the tier — a setting that worked silently.
    expect(catalogSelectionKind(null, 'fast')).toBe('tier')
    expect(catalogSelectionKind(null, 'high')).toBe('tier')
  })

  it('treats no model and no tier as the server default', () => {
    expect(catalogSelectionKind(null, null)).toBe('server-default')
    expect(catalogSelectionKind(undefined, undefined)).toBe('server-default')
    // '' is how the preference spells "no preference".
    expect(catalogSelectionKind(null, '')).toBe('server-default')
  })

  it('lets an explicit model win over a tier', () => {
    // The exclusivity contract in the reducer clears the tier on a model pick,
    // but the display must not depend on that having happened already.
    expect(catalogSelectionKind('claude-opus-4-8', 'fast')).toBe('model')
    expect(catalogSelectionKind('claude-opus-4-8', null)).toBe('model')
  })
})

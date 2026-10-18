import { describe, expect, it } from 'vitest'

import { countLabel } from './plural'

describe('countLabel', () => {
  it('takes the singular for exactly one and the plural for zero and many', () => {
    expect(countLabel(0, '1 Quelle', '{count} Quellen')).toBe('0 Quellen')
    expect(countLabel(1, '1 Quelle', '{count} Quellen')).toBe('1 Quelle')
    expect(countLabel(2, '1 Quelle', '{count} Quellen')).toBe('2 Quellen')
  })

  it('fills the count in the singular as well', () => {
    // Some singulars carry the placeholder ("Durchsuche {count} Sammlung…").
    expect(countLabel(1, 'Durchsuche {count} Sammlung', 'Durchsuche {count} Sammlungen'))
      .toBe('Durchsuche 1 Sammlung')
  })

  it('writes the number as the surface formats it but chooses the form by the count', () => {
    expect(countLabel(1234, '1 Zeichen', '{count} Zeichen', '1.234')).toBe('1.234 Zeichen')
    expect(countLabel(1, '{count} character', '{count} characters', '1')).toBe('1 character')
  })
})

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { controlBoundsViolations } from './layout.ts'

test('a partially visible control remains in bounds analysis when its center is offscreen', () => {
  const violations = controlBoundsViolations([
    {
      bounds: { bottom: 40, left: -80, right: 10, top: 10 },
      name: 'partially-visible-control',
    },
    {
      bounds: { bottom: 40, left: 20, right: 100, top: 10 },
      name: 'contained-control',
    },
  ], {
    bottom: 100,
    left: 0,
    right: 200,
    top: 0,
  }, {
    height: 100,
    width: 200,
  })

  assert.deepEqual(violations, ['partially-visible-control'])
})

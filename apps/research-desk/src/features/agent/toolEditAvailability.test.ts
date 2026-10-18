import { describe, expect, it } from 'vitest'

import { toolEditIsBlockedByMultiAction } from './ComposerGateTray'

/**
 * A multi-action gate cannot be edited — the server says so
 * (`_validated_tool_edit`), because the HITL resume contract carries one
 * action per decision and swapping a tool would grant something the gate
 * never showed. A simply absent control would read as an oversight
 * rather than a rule.
 */
describe('toolEditIsBlockedByMultiAction', () => {
  it('marks a multi-action gate so the control can state the rule', () => {
    expect(toolEditIsBlockedByMultiAction(true, 2)).toBe(true)
  })

  it('leaves a single-action gate to the normal editable path', () => {
    expect(toolEditIsBlockedByMultiAction(true, 1)).toBe(false)
  })

  it('promises nothing to a reader who may not decide at all', () => {
    // Without permission the control stays hidden: a disabled button
    // would advertise a capability this reader does not have.
    expect(toolEditIsBlockedByMultiAction(false, 3)).toBe(false)
  })

  it('says nothing about a gate with no proposed action', () => {
    expect(toolEditIsBlockedByMultiAction(true, 0)).toBe(false)
  })
})

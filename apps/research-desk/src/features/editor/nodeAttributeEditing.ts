/** Attribute changes use the existing plain-edit contract. Suggestion and
 * comment modes cannot represent them as tracked text edits. */
export function nodeAttributeEditingState(
  writeMode: string | undefined,
  editable: boolean,
): { enabled: boolean; reason: 'mode' | 'readonly' | null } {
  if (!editable) return { enabled: false, reason: 'readonly' }
  if (writeMode === 'suggest' || writeMode === 'comment') {
    return { enabled: false, reason: 'mode' }
  }
  return { enabled: true, reason: null }
}

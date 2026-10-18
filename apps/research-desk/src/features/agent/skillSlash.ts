/**
 * `/`-menu detection for the agent composer — the
 * slash twin of `detectCollectionMention` (same adapter pattern; the
 * shared composer mention module stays untouched). A slash token only
 * counts at the start of the text or after whitespace, so paths and
 * URLs never open the menu.
 */

export type SkillSlashState = {
  /** Char index of the `/` in the textarea value. */
  start: number
  query: string
}

export function detectSkillSlash(
  value: string,
  caret: number,
): SkillSlashState | null {
  const safeCaret = Math.max(0, Math.min(caret, value.length))
  const beforeCaret = value.slice(0, safeCaret)
  const match = /(?:^|\s)\/([a-z0-9-]*)$/i.exec(beforeCaret)
  if (!match) return null
  return {
    query: match[1].toLowerCase(),
    start: safeCaret - match[1].length - 1,
  }
}

/** The composer's direct commands: `/web` switches the session's web search
 * on, `/wissen` switches the session's project knowledge on (every visible
 * collection, until the knowledge glyph switches it off), `/canvas` pins the
 * response form to the canvas document. They are neither skills nor tool
 * hints, and never a second depth control.
 *
 * There is no `/chat` counterpart: the agent never raises a canvas
 * unasked, so there is no canvas to escape. The response form in the
 * run-setup menu still offers `chat` explicitly. */
export const COMPOSER_COMMAND_OPTIONS = [
  { id: 'web', token: 'web' },
  { id: 'wissen', token: 'wissen' },
  { id: 'canvas', token: 'canvas' },
] as const

export type ComposerCommandId = (typeof COMPOSER_COMMAND_OPTIONS)[number]['id']

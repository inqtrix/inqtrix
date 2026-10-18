import type { TranslationDictionary } from '@/i18n/translations'

/**
 * The failure of a run, in the reader's language.
 *
 * The backend's failure field carries two different things: stable
 * identifiers (`all_tasks_failed`) and text that is already a German
 * sentence (a task's own result summary). Rendering whichever arrived
 * would tell a user "all_tasks_failed" — a word from inside the machine,
 * with no hint of what it meant or what had still succeeded.
 *
 * Mapping is by code, and anything unknown passes through UNCHANGED.
 * A failure the UI cannot name is still a failure the user must see:
 * swallowing it, or replacing it with a generic apology, would hide the
 * one string that says what went wrong.
 */
export function agentRunFailureText(
  error: string,
  t: TranslationDictionary,
  errorCode?: string,
): string {
  const codes = t.agent.timeline.failureCodes as Record<string, string>
  if (errorCode && codes[errorCode]) return codes[errorCode]
  const raw = error.trim()
  if (!raw) return raw
  const known = codes[raw]
  if (known) return known
  // `code: detail` — the planner reports invalid plans that way. Keep
  // the detail: it names the rule that was broken.
  const separator = raw.indexOf(': ')
  if (separator > 0) {
    const prefix = codes[raw.slice(0, separator)]
    if (prefix) return `${prefix} ${raw.slice(separator + 2)}`
  }
  return raw
}

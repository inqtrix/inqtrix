/**
 * Owner / local-account password policy (pure logic, UI-agnostic).
 *
 * The ONLY hard requirement is the minimum length — it mirrors the
 * server's `_MIN_PASSWORD_LEN` exactly, so the UI never silently rejects a
 * password the backend would accept, nor vice versa. The character-class
 * checks are advisory strength hints that feed the meter; they are NOT
 * gates (over-restricting beyond the server would be an invisible
 * divergence). Keep `MIN_PASSWORD_LENGTH` in sync with the backend.
 */
export const MIN_PASSWORD_LENGTH = 12

export type PasswordCheckId = 'length' | 'lower' | 'upper' | 'digit'

export type PasswordCheck = {
  id: PasswordCheckId
  /** True when the rule is required to submit (length only). */
  required: boolean
  met: boolean
}

export type PasswordStrength = 'empty' | 'weak' | 'fair' | 'strong'

/** The checklist shown beside the field; `length` is the only gate. */
export function passwordChecks(password: string): PasswordCheck[] {
  return [
    { id: 'length', required: true, met: password.length >= MIN_PASSWORD_LENGTH },
    { id: 'lower', required: false, met: /[a-z]/.test(password) },
    { id: 'upper', required: false, met: /[A-Z]/.test(password) },
    { id: 'digit', required: false, met: /\d/.test(password) },
  ]
}

/** Whether the password may be submitted (server-aligned: length only). */
export function isPasswordAcceptable(password: string): boolean {
  return password.length >= MIN_PASSWORD_LENGTH
}

/** Coarse strength for the meter — never blocks submission on its own. */
export function passwordStrength(password: string): PasswordStrength {
  if (password.length === 0) return 'empty'
  if (!isPasswordAcceptable(password)) return 'weak'
  const advisoryMet = passwordChecks(password).filter(
    (check) => !check.required && check.met,
  ).length
  if (advisoryMet >= 3) return 'strong'
  if (advisoryMet >= 1) return 'fair'
  return 'weak'
}

/** Whether *password* and its confirmation match (both non-empty). */
export function passwordsMatch(password: string, confirm: string): boolean {
  return password.length > 0 && password === confirm
}

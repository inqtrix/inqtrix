/**
 * Whether the gate's browser contexts and API sessions accept the stack's
 * certificate.
 *
 * The HTTPS stack serves a throwaway certificate, and the gate sets
 * INQTRIX_E2E_IGNORE_HTTPS_ERRORS=1 for it. Every context and session takes
 * the switch from here: one that derived it on its own, or forgot it, fails
 * on the certificate alone while the others pass.
 *
 * @param {Record<string, string | undefined>} [environment]
 * @returns {boolean}
 */
export function httpsErrorsIgnored(environment = process.env) {
  return environment.INQTRIX_E2E_IGNORE_HTTPS_ERRORS === '1'
}

/**
 * Send, then settle what the draft carried — never the other way round.
 *
 * Rules, skills, attached reports and the canvas pin hold for ONE task: an
 * accepted submission clears them, a refused one keeps everything the user
 * assembled for the retry. Settling before the server answered would clear
 * them for a send that is then refused. A send that returns nothing counts
 * as refused (the caller reports it as not sent), and a send that throws
 * settles nothing: the error surfaces and the draft stays as it was.
 */
export async function sendThenSettle<T>(
  send: () => Promise<T> | T,
  settle: (accepted: boolean) => void,
): Promise<T> {
  const result = await send()
  settle(result !== null && result !== undefined)
  return result
}

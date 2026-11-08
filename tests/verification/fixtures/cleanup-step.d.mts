export function attemptCleanup(
  label: string,
  step: () => unknown,
  write?: (line: string) => void,
): Promise<boolean>

export function ignoreMissing(error: unknown): void

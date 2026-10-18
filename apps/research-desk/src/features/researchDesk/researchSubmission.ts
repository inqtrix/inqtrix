export type ResearchSubmissionOutcome =
  | { status: 'accepted' }
  | {
      message: string
      recoverability: 'login' | 'retry'
      status: 'rejected'
    }

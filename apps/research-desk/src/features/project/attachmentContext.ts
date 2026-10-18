import type { ChatMessageAttachmentRecord } from './types'

/**
 * Build the `@mention`-style label that heads an attachment's context block.
 *
 * Mirrors the token the user typed in the composer (e.g. `@files:report`,
 * `@filegroups:dossier`) so the model can map each `[N]` block back to the
 * reference in the instruction text. A research report may carry no explicit
 * label, in which case its title stands in.
 */
export function attachmentMentionLabel(attachment: ChatMessageAttachmentRecord): string {
  switch (attachment.kind) {
    case 'chat-rule':
      return `@rules:${attachment.label}`
    case 'research-report':
      return `@research:${attachment.label ?? attachment.title}`
    case 'file-asset':
      return `@files:${attachment.label}`
    case 'file-group':
      return `@filegroups:${attachment.groupLabel}`
  }
}

/**
 * A logical reference unit: exactly one `[N]` the user typed in the composer.
 *
 * `groupId` is non-null only for a file group; its members share one number.
 */
type AttachmentUnit = {
  groupId: string | null
  members: ChatMessageAttachmentRecord[]
}

/**
 * Collapse the flat, expanded attachment list back into one unit per pill.
 *
 * `chatAttachmentsFromRefs` expands a file-group reference into one record per
 * member, all sharing the same `groupId` and emitted contiguously, while every
 * other attachment is standalone. Numbering must follow the *pill* the user
 * typed (a group is a single `[N]`), not the expanded member count: otherwise a
 * multi-member group mints extra block numbers and shifts every later pill, so
 * the instruction's `[N]` no longer points at the right block and the model only
 * "sees" the first group member. Grouping by *consecutive* same `groupId`
 * reconstructs the units in their original order, keeping block numbers aligned
 * with the instruction.
 */
function groupAttachmentUnits(attachments: ChatMessageAttachmentRecord[]): AttachmentUnit[] {
  const units: AttachmentUnit[] = []
  for (const attachment of attachments) {
    const groupId = attachment.kind === 'file-group' ? attachment.groupId : null
    const current = units.at(-1)
    if (groupId !== null && current && current.groupId === groupId) {
      current.members.push(attachment)
      continue
    }
    units.push({ groupId, members: [attachment] })
  }
  return units
}

/**
 * Render one member's body: a `Title:` line plus its full content.
 *
 * No silent per-attachment truncation: the whole content is
 * sent. The composer token meter signals when the combined context exceeds the
 * selected model's window, and the backend re-clamps visibly as the last
 * resort.
 */
function memberBlock(member: ChatMessageAttachmentRecord): string {
  return [`Title: ${member.title}`, member.contentMarkdown].join('\n')
}

/**
 * Inline the attached context blocks into the user message.
 *
 * Emits one numbered block per logical reference unit (a file group's members
 * merge into a single `[N]` block, sub-headed per document) so the block numbers
 * line up with the `[N]` markers the composer wrote into the instruction. The
 * aggregate size guardrail is the visible >50%-context warning above the
 * composer; here each member only carries its own per-document cap.
 */
export function contentWithAttachmentContext(
  contentMarkdown: string,
  attachments: ChatMessageAttachmentRecord[],
): string {
  const blocks = renderAttachmentContextBlocks(attachments)

  return [
    'Use the attached Inqtrix chat context blocks for this answer.',
    '',
    blocks,
    '',
    'User message:',
    contentMarkdown,
  ].join('\n')
}

export function renderAttachmentContextBlocks(
  attachments: ChatMessageAttachmentRecord[],
): string {
  return groupAttachmentUnits(attachments).map((unit, index) => {
    const number = index + 1
    const label = attachmentMentionLabel(unit.members[0])
    const header = unit.members.length > 1
      ? `--- [${number}] ${label} (${unit.members.length} documents) ---`
      : `--- [${number}] ${label} ---`
    return [
      header,
      unit.members.map(memberBlock).join('\n\n'),
      `--- End context ${number} ---`,
    ].join('\n')
  }).join('\n\n')
}

import type {
  FileLibrarySectionRecord,
  FileSectionSemanticRole,
} from '@/features/project/types'
import { createProjectEntityId } from '@/features/project/entityId'

export const LEGACY_FILE_SECTION_IDS = [
  'file-section-temp',
  'file-section-library',
  'file-section-sources',
] as const

const DEFAULT_FILE_SECTION_SIGNATURES = new Set([
  'temporary:Temporäre Dateien',
  'custom:Bibliothek',
  'custom:Projekt-Quellen',
])

export function createFileSectionId(): string {
  return createProjectEntityId('file-section')
}

/**
 * Build the three prepared library sections. Titles are plain, renameable
 * strings (German defaults, matching the app's default locale) — once renamed
 * they become user-owned data, so they are not resolved through i18n.
 */
export function createDefaultFileLibrarySections(now: string): FileLibrarySectionRecord[] {
  return [
    { createdAt: now, id: createFileSectionId(), isBootstrapPlaceholder: true, kind: 'temporary', semanticRole: 'temporary', title: 'Temporäre Dateien', updatedAt: now },
    { createdAt: now, id: createFileSectionId(), isBootstrapPlaceholder: true, kind: 'custom', semanticRole: 'library', title: 'Bibliothek', updatedAt: now },
    { createdAt: now, id: createFileSectionId(), isBootstrapPlaceholder: true, kind: 'custom', semanticRole: 'project_sources', title: 'Projekt-Quellen', updatedAt: now },
  ]
}

export function isPristineDefaultFileSection(
  section: FileLibrarySectionRecord,
): boolean {
  return section.createdAt === section.updatedAt
    && DEFAULT_FILE_SECTION_SIGNATURES.has(`${section.kind}:${section.title}`)
}

export function temporaryFileSectionId(
  sections: Iterable<FileLibrarySectionRecord>,
): string {
  for (const section of sections) {
    if (section.kind === 'temporary') return section.id
  }
  throw new Error('File library has no temporary section.')
}

export function legacyFileSectionIdReplacements(
  sections: Record<string, FileLibrarySectionRecord>,
  serverIds: ReadonlySet<string>,
): Record<string, string> {
  const occupied = new Set([...Object.keys(sections), ...serverIds])
  const replacements: Record<string, string> = {}
  for (const legacyId of LEGACY_FILE_SECTION_IDS) {
    if (!sections[legacyId] || serverIds.has(legacyId)) continue
    let replacement = createFileSectionId()
    while (occupied.has(replacement)) replacement = createFileSectionId()
    occupied.add(replacement)
    replacements[legacyId] = replacement
  }
  return replacements
}

const PREPARED_ROLES: ReadonlySet<FileSectionSemanticRole> = new Set([
  'temporary',
  'library',
  'project_sources',
])

/**
 * Redirect local bootstrap IDs to the server's canonical prepared-role IDs.
 *
 * A user-created/renamed section is never rekeyed merely because its title
 * matches a default. Only an explicit local bootstrap role participates.
 */
export function defaultFileSectionIdReplacements(
  sections: Record<string, FileLibrarySectionRecord>,
  canonicalSections: readonly FileLibrarySectionRecord[],
): Record<string, string> {
  const canonicalByRole = new Map(
    canonicalSections.flatMap((section) => (
      section.semanticRole && PREPARED_ROLES.has(section.semanticRole)
        ? [[section.semanticRole, section.id] as const]
        : []
    )),
  )
  return Object.fromEntries(
    Object.values(sections).flatMap((section) => {
      if (
        section.isBootstrapPlaceholder !== true
        || !section.semanticRole
        || !PREPARED_ROLES.has(section.semanticRole)
      ) return []
      const canonicalId = canonicalByRole.get(section.semanticRole)
      return canonicalId && canonicalId !== section.id
        ? [[section.id, canonicalId] as const]
        : []
    }),
  )
}

/**
 * Derived file names for canvas documents.
 *
 * The name is DISPLAY AND ADDRESS, never a key: identity stays the
 * artifactId, the name is `slug(title) + ".md"` made unique per session
 * with a `-2` suffix in created order. Byte-exact port of the Python
 * reference (`src/inqtrix/agents/artifact_names.py`), pinned by the
 * shared fixture `tests/fixtures/artifact_name_parity.json` — change
 * one side only via the fixture.
 */

export const ARTIFACT_NAME_FALLBACK = 'dokument'

export function artifactSlug(title: string): string {
  // normalize(NFKD) -> toLowerCase -> strip combining marks ->
  // non-alnum runs to '-' -> strip edge dashes -> THEN slice to 48
  // (a slice landing on a dash keeps it, exactly like the Python side).
  // A title that already carries the extension ("bericht.md") must not
  // become "bericht-md.md": strip ONE trailing ".md" before slugging.
  const normalized = title
    .replace(/\s*\.md\s*$/i, '')
    .normalize('NFKD')
    .toLowerCase()
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 48)
  return normalized || ARTIFACT_NAME_FALLBACK
}

/**
 * Map artifactId -> file name for documents in CREATED order (the
 * session index's `order` contract, oldest first). Collisions get
 * `-2`, `-3`, ... BEFORE the extension (`bericht-2.md`). Renaming an
 * OLDER document can shift a younger namesake's suffix: accepted,
 * because the name is display and the id stays stable.
 */
export function assignArtifactFileNames(
  items: readonly { artifactId: string; title: string }[],
): Record<string, string> {
  const taken = new Set<string>()
  const names: Record<string, string> = {}
  for (const item of items) {
    const base = artifactSlug(item.title)
    let candidate = base
    let suffix = 2
    while (taken.has(candidate)) {
      candidate = `${base}-${suffix}`
      suffix += 1
    }
    taken.add(candidate)
    names[item.artifactId] = `${candidate}.md`
  }
  return names
}

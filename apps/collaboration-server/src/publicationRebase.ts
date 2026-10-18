import type { Node as ProseMirrorNode } from '@tiptap/pm/model'
import { StepMap } from '@tiptap/pm/transform'
import { diffArrays } from 'diff'

import { CloseCodes, CollaborationError } from './errors'

/**
 * Placing an AI publication onto the shared document as it is now.
 *
 * The browser builds a publication's target on the projection it read before
 * the click (the base). When other writers changed the document since, the
 * target cannot be published as it is: compared with the current state, their
 * changes would read as part of the AI's change. This module moves the AI's
 * change onto the current state instead, the way an operational-transform
 * server moves a late change over everything since its base, and as a
 * three-way merge does without an operation log:
 *
 * - base → target is the AI section: the one range that differs, with the
 *   content the target puts there;
 * - base → current are the changes of the other writers, found per leaf
 *   block (textblocks and leaf blocks) and, inside a block whose shell stayed
 *   the same, per character;
 * - the AI section is placed where those changes moved it, unless one of
 *   them touches it. Touching includes an insertion exactly at either edge:
 *   then the passage the AI rewrote is no longer the one it read, and the
 *   publication is refused as `base_changed`, with nothing stored.
 *
 * Without an operation log, one current state can come from more than one
 * change: text typed again next to the same text reads as typed before or
 * after it, and two blocks moved past each other while both were edited read
 * as both edited in place. When text typed or removed that way may sit at
 * the AI section, or when swapping the AI section's block with another
 * changed block explains the current state with as small a change, the
 * publication is refused as well, instead of placed by the reading the
 * difference happened to find first.
 *
 * Changes elsewhere, also in the same paragraph or sentence, are kept as they
 * are; the result is still a suggestion a reviewer decides on, exactly as
 * when the same change had been typed a moment after the publication.
 */

/** The most leaf-block insertions and removals between the base and the
 * current state for which the alignment is computed. Beyond it the
 * publication is refused as `base_changed` (`base_too_far`); the client
 * builds it again on the current projection. */
export const MAX_REBASE_LEAF_EDITS = 200

export type PublicationPlacement = {
  /** `direct`: the current state is the base, and the target is published
   * as it is. `rebased`: the AI section was placed onto a current state that
   * differs from the base. */
  outcome: 'direct' | 'rebased'
  target: ProseMirrorNode
}

export type BaseChangedReason = 'base_ambiguous' | 'base_too_far' | 'hunk_touched'

/** One textblock or leaf block, with what identifies it across documents:
 * its own JSON and the type and attributes of every block around it. */
type Leaf = {
  /** The type and attributes of the enclosing blocks, as JSON. */
  chain: string
  from: number
  key: string
  node: ProseMirrorNode
  to: number
}

/** A change of the current state against the base: the base range
 * `[from, to]` became `size` positions in the current state. */
type ChangedRange = {
  from: number
  size: number
  to: number
}

/** What the other writers changed: the ranges, and every changed textblock
 * paired by position (removed with added), across the whole document, since
 * another order of the same blocks might explain a pairing as well. */
type Changes = {
  pairs: Array<[Leaf, Leaf]>
  ranges: ChangedRange[]
}

type Shield = {
  from: number
  reason: Exclude<BaseChangedReason, 'base_too_far'>
  to: number
}

/**
 * The target to publish on *current*: *target* itself when nothing changed
 * since *base*, otherwise *current* with the AI section of *base* → *target*
 * placed where the changes since moved it.
 *
 * All three documents are final projections in the publisher's schema.
 * Throws `base_changed` (409) when the changes since the base touch the AI
 * section or may be read so, and `internal_consistency` when a placement
 * would replace other content than the AI replaced in its base; neither
 * stores anything.
 */
export function rebasePublication(
  base: ProseMirrorNode,
  target: ProseMirrorNode,
  current: ProseMirrorNode,
): PublicationPlacement {
  if (base.eq(current)) return { outcome: 'direct', target }
  const section = aiSection(base, target)
  // Nothing to place: the publisher refuses a target equal to the document.
  if (!section) return { outcome: 'rebased', target: current }

  const baseLeaves = leavesOf(base)
  const currentLeaves = leavesOf(current)
  const changes = changedRanges(base, current, baseLeaves, currentLeaves)
  if (!changes) throw baseChanged('base_too_far')
  const { ranges } = changes
  for (const shield of shieldsOf(section, baseLeaves, currentLeaves)) {
    if (ranges.some((range) => range.from <= shield.to && range.to >= shield.from)) {
      throw baseChanged(shield.reason)
    }
  }
  if (pairedAmbiguously(section, changes.pairs)) throw baseChanged('base_ambiguous')

  const map = new StepMap(ranges.flatMap((range) => [range.from, range.to - range.from, range.size]))
  const from = map.map(section.from, 1)
  const to = map.map(section.toBase, -1)
  if (!replacesBaseContent(current, from, to, base, section.from, section.toBase)) {
    throw placementInconsistent()
  }
  let placed: ProseMirrorNode
  try {
    placed = current.replace(from, to, target.slice(section.from, section.toTarget))
    placed.check()
  } catch {
    throw placementInconsistent()
  }
  return { outcome: 'rebased', target: placed }
}

/**
 * Whether `current[from, to]` holds exactly what `base[baseFrom, baseTo]`
 * holds, together with one position on each side, open depths included: a
 * placement replaces the text the AI replaced in its base, never other text,
 * and lands between the same neighbours. A mapping that is off by one
 * position fails here instead of replacing a neighbour's letter, also for a
 * pure insertion, which replaces nothing. At the start or the end of the
 * document its edge is that position. Every change touching these positions
 * touches the AI section and was refused before, so for a right mapping this
 * always holds.
 */
export function replacesBaseContent(
  current: ProseMirrorNode,
  from: number,
  to: number,
  base: ProseMirrorNode,
  baseFrom: number,
  baseTo: number,
): boolean {
  const before = baseFrom > 0 ? 1 : 0
  const after = baseTo < base.content.size ? 1 : 0
  if ((before === 0 && from !== 0) || (after === 0 && to !== current.content.size)) return false
  try {
    return current.slice(from - before, to + after).eq(base.slice(baseFrom - before, baseTo + after))
  } catch {
    return false
  }
}

/** The one range in which *target* differs from *base*, as the publisher's
 * own difference finds it. */
function aiSection(
  base: ProseMirrorNode,
  target: ProseMirrorNode,
): { from: number; toBase: number; toTarget: number } | null {
  const from = base.content.findDiffStart(target.content)
  const ends = base.content.findDiffEnd(target.content)
  if (from === null || ends === null) return null
  let toBase = ends.a
  let toTarget = ends.b
  const overlap = from - Math.min(toBase, toTarget)
  if (overlap > 0) {
    toBase += overlap
    toTarget += overlap
  }
  return { from, toBase, toTarget }
}

function leavesOf(document: ProseMirrorNode): Leaf[] {
  const leaves: Leaf[] = []
  document.descendants((node, position) => {
    if (!node.isTextblock && !(node.isBlock && node.isLeaf)) return true
    const resolved = document.resolve(position)
    const ancestors: Array<[string, Record<string, unknown>]> = []
    for (let depth = 1; depth <= resolved.depth; depth += 1) {
      const ancestor = resolved.node(depth)
      ancestors.push([ancestor.type.name, ancestor.attrs])
    }
    leaves.push({
      chain: JSON.stringify(ancestors),
      from: position,
      key: JSON.stringify([ancestors, node.toJSON()]),
      node,
      to: position + node.nodeSize,
    })
    return false
  })
  return leaves
}

/**
 * What the other writers changed, as base ranges. Leaves are aligned by
 * their keys (Myers, bounded by MAX_REBASE_LEAF_EDITS). Between two aligned
 * leaves, a changed gap or the leaves in it form one range, except where as
 * many leaves were removed as added and each pair has the same shell (type,
 * attributes, enclosing blocks, structure around it): there the change is
 * found per character inside each pair, so typing in one paragraph never
 * covers the paragraph next to it.
 */
function changedRanges(
  base: ProseMirrorNode,
  current: ProseMirrorNode,
  baseLeaves: readonly Leaf[],
  currentLeaves: readonly Leaf[],
): Changes | null {
  const parts = diffArrays(
    baseLeaves.map((leaf) => leaf.key),
    currentLeaves.map((leaf) => leaf.key),
    { maxEditLength: MAX_REBASE_LEAF_EDITS },
  )
  if (!parts) return null
  type Anchor = { baseFrom: number; baseIndex: number; baseTo: number; currentFrom: number; currentIndex: number; currentTo: number }
  const anchors: Anchor[] = [{ baseFrom: 0, baseIndex: -1, baseTo: 0, currentFrom: 0, currentIndex: -1, currentTo: 0 }]
  let baseIndex = 0
  let currentIndex = 0
  for (const part of parts) {
    if (part.added) {
      currentIndex += part.count
    } else if (part.removed) {
      baseIndex += part.count
    } else {
      for (let offset = 0; offset < part.count; offset += 1) {
        const baseLeaf = baseLeaves[baseIndex + offset]!
        const currentLeaf = currentLeaves[currentIndex + offset]!
        anchors.push({
          baseFrom: baseLeaf.from,
          baseIndex: baseIndex + offset,
          baseTo: baseLeaf.to,
          currentFrom: currentLeaf.from,
          currentIndex: currentIndex + offset,
          currentTo: currentLeaf.to,
        })
      }
      baseIndex += part.count
      currentIndex += part.count
    }
  }
  anchors.push({
    baseFrom: base.content.size,
    baseIndex: baseLeaves.length,
    baseTo: base.content.size,
    currentFrom: current.content.size,
    currentIndex: currentLeaves.length,
    currentTo: current.content.size,
  })

  const ranges: ChangedRange[] = []
  const pairedLeaves: Array<[Leaf, Leaf]> = []
  for (let index = 0; index + 1 < anchors.length; index += 1) {
    const previous = anchors[index]!
    const next = anchors[index + 1]!
    const baseGap = { from: previous.baseTo, to: next.baseFrom }
    const currentGap = { from: previous.currentTo, to: next.currentFrom }
    const removed = baseLeaves.slice(previous.baseIndex + 1, next.baseIndex)
    const added = currentLeaves.slice(previous.currentIndex + 1, next.currentIndex)
    if (removed.length === 0 && added.length === 0) {
      if (baseGap.to - baseGap.from !== currentGap.to - currentGap.from) {
        ranges.push({ from: baseGap.from, size: currentGap.to - currentGap.from, to: baseGap.to })
      }
      continue
    }
    const pairs = sameShellPairs(removed, added, baseGap, currentGap)
    if (pairs) {
      for (const [removedLeaf, addedLeaf] of pairs) {
        const difference = textDifference(removedLeaf, addedLeaf)
        if (difference) ranges.push(difference.range)
      }
      pairedLeaves.push(...pairs)
      continue
    }
    ranges.push({ from: baseGap.from, size: currentGap.to - currentGap.from, to: baseGap.to })
  }
  return { pairs: pairedLeaves, ranges }
}

/** Whether a changed textblock may stand for another: same type,
 * attributes and enclosing blocks. */
function sameShell(removedLeaf: Leaf, addedLeaf: Leaf): boolean {
  return removedLeaf.node.isTextblock
    && removedLeaf.node.type.name === addedLeaf.node.type.name
    && removedLeaf.chain === addedLeaf.chain
    && JSON.stringify(removedLeaf.node.attrs) === JSON.stringify(addedLeaf.node.attrs)
}

/** Pairs of removed and added textblocks that differ only inside, or null. */
function sameShellPairs(
  removed: readonly Leaf[],
  added: readonly Leaf[],
  baseGap: { from: number; to: number },
  currentGap: { from: number; to: number },
): Array<[Leaf, Leaf]> | null {
  if (removed.length === 0 || removed.length !== added.length) return null
  const pairs: Array<[Leaf, Leaf]> = []
  for (let index = 0; index < removed.length; index += 1) {
    const removedLeaf = removed[index]!
    const addedLeaf = added[index]!
    const removedBefore = removedLeaf.from - (index === 0 ? baseGap.from : removed[index - 1]!.to)
    const addedBefore = addedLeaf.from - (index === 0 ? currentGap.from : added[index - 1]!.to)
    if (!sameShell(removedLeaf, addedLeaf) || removedBefore !== addedBefore) return null
    pairs.push([removedLeaf, addedLeaf])
  }
  const removedAfter = baseGap.to - removed[removed.length - 1]!.to
  const addedAfter = currentGap.to - added[added.length - 1]!.to
  return removedAfter === addedAfter ? pairs : null
}

/**
 * How two textblocks of the same shell differ, or null when their content
 * is equal: the base range that changed, and `size`, the positions the
 * smallest such change removes and inserts.
 *
 * When the common start and the common end overlap, the change repeats text
 * next to it ("die " typed beside "die") and could have been made anywhere
 * along that repetition. The range then covers every such reading instead of
 * the first one the difference finds, so that a reading at an edge of the AI
 * section is never ruled out.
 */
function textDifference(baseLeaf: Leaf, currentLeaf: Leaf): { range: ChangedRange; size: number } | null {
  const start = baseLeaf.node.content.findDiffStart(currentLeaf.node.content)
  if (start === null) return null
  const ends = baseLeaf.node.content.findDiffEnd(currentLeaf.node.content)
  if (ends === null) return null
  const overlap = Math.max(0, start - Math.min(ends.a, ends.b))
  const from = start - overlap
  const endBase = ends.a + overlap
  const endCurrent = ends.b + overlap
  return {
    range: {
      from: baseLeaf.from + 1 + from,
      size: endCurrent - from,
      to: baseLeaf.from + 1 + endBase,
    },
    size: endBase - start + (endCurrent - start),
  }
}

/** The positions the smallest change of *removedLeaf* into *addedLeaf*
 * removes and inserts. */
function changeSize(removedLeaf: Leaf, addedLeaf: Leaf): number {
  return textDifference(removedLeaf, addedLeaf)?.size ?? 0
}

/**
 * Whether a textblock the AI section lies in was paired by position with a
 * changed block although swapping its partner with that of another pair of
 * the same shell explains the current state with as small a change: two
 * paragraphs moved past each other while both were edited read the same as
 * both edited in place, and the AI change would land in the other one. The
 * other pair may stand anywhere in the document: blocks that stayed between
 * the two moved ones only split them into separate changes.
 */
function pairedAmbiguously(
  section: { from: number; toBase: number },
  pairs: ReadonlyArray<[Leaf, Leaf]>,
): boolean {
  for (let index = 0; index < pairs.length; index += 1) {
    const [removedLeaf, addedLeaf] = pairs[index]!
    if (removedLeaf.from > section.toBase || removedLeaf.to < section.from) continue
    for (let other = 0; other < pairs.length; other += 1) {
      if (other === index) continue
      const [otherRemoved, otherAdded] = pairs[other]!
      if (!sameShell(removedLeaf, otherAdded) || !sameShell(otherRemoved, addedLeaf)) continue
      const inPlace = changeSize(removedLeaf, addedLeaf) + changeSize(otherRemoved, otherAdded)
      const swapped = changeSize(removedLeaf, otherAdded) + changeSize(otherRemoved, addedLeaf)
      if (swapped <= inPlace) return true
    }
  }
  return false
}

/**
 * What a change of another writer must not touch: the AI section, edges
 * included. When the leaves the section lies in occur more than once in a
 * row in the current state, the alignment may have matched a twin; then the
 * section's leaves as a whole and the content of their neighbours are
 * protected too, so that no change next to a twin can be taken for one
 * beside the section.
 */
function shieldsOf(
  section: { from: number; toBase: number },
  baseLeaves: readonly Leaf[],
  currentLeaves: readonly Leaf[],
): Shield[] {
  const shields: Shield[] = [{ from: section.from, reason: 'hunk_touched', to: section.toBase }]
  const touchedIndexes = baseLeaves
    .map((leaf, index) => (leaf.from <= section.toBase && leaf.to >= section.from ? index : -1))
    .filter((index) => index >= 0)
  if (touchedIndexes.length === 0) return shields
  const first = touchedIndexes[0]!
  const last = touchedIndexes[touchedIndexes.length - 1]!
  const run = baseLeaves.slice(first, last + 1).map((leaf) => leaf.key)
  let occurrences = 0
  for (let start = 0; start + run.length <= currentLeaves.length && occurrences < 2; start += 1) {
    if (run.every((key, offset) => currentLeaves[start + offset]!.key === key)) occurrences += 1
  }
  if (occurrences < 2) return shields
  shields.push({ from: baseLeaves[first]!.from, reason: 'base_ambiguous', to: baseLeaves[last]!.to })
  const before = baseLeaves[first - 1]
  const after = baseLeaves[last + 1]
  if (before) shields.push({ ...contentRange(before), reason: 'base_ambiguous' })
  if (after) shields.push({ ...contentRange(after), reason: 'base_ambiguous' })
  return shields
}

function contentRange(leaf: Leaf): { from: number; to: number } {
  return leaf.node.isTextblock
    ? { from: leaf.from + 1, to: leaf.to - 1 }
    : { from: leaf.from, to: leaf.to }
}

function baseChanged(reason: BaseChangedReason): CollaborationError {
  return new CollaborationError('base_changed', {
    closeCode: CloseCodes.incompatible,
    httpStatus: 409,
    suggestionReason: reason,
  })
}

function placementInconsistent(): CollaborationError {
  return new CollaborationError('internal_consistency', {
    closeCode: CloseCodes.internalConsistency,
    suggestionReason: 'placement_replaces_other_content',
  })
}

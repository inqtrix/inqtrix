import type { Node as ProseMirrorNode } from '@tiptap/pm/model'
import type { Transaction } from '@tiptap/pm/state'

const ZERO_WIDTH_SPACE = '\u200B'

/**
 * The zero-width spaces that mark proposed block breaks.
 *
 * A proposed break is two adjacent text blocks (nothing but node boundaries
 * between them) whose touching edges carry the same insertion suggestion:
 * the upstream tracker anchors the otherwise empty insertion with one
 * zero-width space on each side of the boundary. Per boundary this returns
 * at most one position on each side: the last zero-width space in the
 * insertion run that ends the left block and the first one in the run that
 * starts the right block. Text typed later can leave an anchor in the middle
 * of a run; it is still found. A zero-width space typed on purpose elsewhere
 * is not returned.
 *
 * `isTarget` selects the suggestions whose breaks are meant, for example the
 * one suggestion being accepted.
 */
export function proposedBlockAnchorPositions(
  document: ProseMirrorNode,
  isTarget: (suggestionId: string) => boolean = () => true,
): number[] {
  const blocks: Array<{ node: ProseMirrorNode; start: number }> = []
  document.descendants((node, position) => {
    if (!node.isTextblock) return true
    blocks.push({ node, start: position + 1 })
    return false
  })
  const insertionId = (node: ProseMirrorNode | null): string | null => {
    if (!node?.isText) return null
    const mark = node.marks.find((candidate) => candidate.type.name === 'insertion')
    const id: unknown = mark ? mark.attrs.suggestionId ?? mark.attrs.id : null
    return typeof id === 'string' && isTarget(id) ? id : null
  }
  const positions = new Set<number>()
  for (let index = 0; index + 1 < blocks.length; index += 1) {
    const left = blocks[index]!
    const right = blocks[index + 1]!
    const leftEnd = left.start + left.node.content.size
    if (!onlyNodeBoundariesBetween(document, leftEnd, right.start)) continue
    const id = insertionId(left.node.lastChild)
    if (!id || insertionId(right.node.firstChild) !== id) continue

    let offset = leftEnd
    for (let child = left.node.childCount - 1; child >= 0; child -= 1) {
      const node = left.node.child(child)
      offset -= node.nodeSize
      if (insertionId(node) !== id) break
      const at = node.text!.lastIndexOf(ZERO_WIDTH_SPACE)
      if (at >= 0) {
        positions.add(offset + at)
        break
      }
    }
    offset = right.start
    for (let child = 0; child < right.node.childCount; child += 1) {
      const node = right.node.child(child)
      if (insertionId(node) !== id) break
      const at = node.text!.indexOf(ZERO_WIDTH_SPACE)
      if (at >= 0) {
        positions.add(offset + at)
        break
      }
      offset += node.nodeSize
    }
  }
  return [...positions].sort((left, right) => left - right)
}

/** Adds the deletion of every proposed-break anchor to the transaction. */
export function addProposedBlockAnchorCleanup(
  transaction: Transaction,
  isTarget: (suggestionId: string) => boolean = () => true,
): Transaction {
  const positions = proposedBlockAnchorPositions(transaction.doc, isTarget)
  for (const position of positions.reverse()) {
    transaction.delete(position, position + 1)
  }
  return transaction
}

function onlyNodeBoundariesBetween(
  document: ProseMirrorNode,
  from: number,
  to: number,
): boolean {
  let leaf = false
  document.nodesBetween(from, to, (node, position) => {
    if (leaf) return false
    if (position >= from && node.isLeaf) leaf = true
    return !leaf
  })
  return !leaf
}

import { expect, test } from '@playwright/test'
import { parseEditorMarkdown } from '@inqtrix/editor-schema'
import type { JSONContent } from '@tiptap/core'

import {
  CollaborationStack,
  WRITER_A,
  WRITER_B,
  closeWriters,
  describeUpdate,
  editorText,
  expectEditorSelectionFollowsPage,
  expectSaved,
  openWriter,
  placeCaretAtBlockEnd,
  projectionText,
  settledDurability,
  suggestionsOf,
  typeSlowly,
  writerDocument,
  type WriterPage,
} from './collaborationStack'

// Suggest mode with real keys against the real collaboration service. Every
// action a writer with suggest access takes ends in exactly one of two ways:
// the editor refuses it before anything is sent and says why in German, or
// the service stores what the editor sent. A change the service refuses
// never leaves the editor, and suggest access never changes the original
// document.

/** What the editor says for each refusal reason; a reword must be caught
 * here. */
const REFUSAL_TEXT = {
  adjacent_own_suggestion_conflict: 'Diese Änderung schließt direkt an einen anderen offenen Vorschlag von Ihnen an und lässt sich nicht mit ihm verbinden. Ändern Sie an anderer Stelle.',
  foreign_collision: 'Diese Bearbeitung überschneidet sich mit einer Änderung einer anderen Person. Prüfen Sie die markierte Änderung zuerst.',
  foreign_suggestion_changed: 'Diese Änderung würde einen offenen Vorschlag einer anderen Person verändern. Ändern Sie an anderer Stelle oder warten Sie, bis über den Vorschlag entschieden ist.',
  format_overlaps_own_suggestion: 'Diese Formatierung würde Ihren eigenen offenen Vorschlag verändern. Formatieren Sie nur innerhalb Ihres Vorschlags oder nur außerhalb davon.',
  node_marks_unsupported: 'Diese Änderung betrifft einen ganzen Block oder ein eingebettetes Element und lässt sich nicht als Vorschlag darstellen.',
  split_moves_content: 'Im Modus „Vorschlagen“ kann ein neuer Absatz nur am Ende eines Absatzes beginnen. Setzen Sie den Cursor ans Absatzende.',
  suggestion_would_disappear: 'Damit würde Ihr eigener offener Vorschlag ganz entfernt. Ein Vorschlag lässt sich nur zurückziehen, nicht löschen.',
  unsupported_structure: 'Diese Änderung lässt sich nicht als Vorschlag darstellen.',
} as const

type RefusalReason = keyof typeof REFUSAL_TEXT

const CATALOG_MARKDOWN = [
  '# Heading ZONEHEAD',
  'First paragraph ZONEONE ends here',
  'Second paragraph ZONETWO ends here',
  'Paragraph with a foreign suggestion ZONEFOREIGN',
  'Paragraph with an own suggestion ZONEOWN',
  'Paragraph with a **bold** word ZONEBOLD',
  '- List item ZONELIST\n- List item with a nested list\n  - Nested item ZONENESTED',
  '1. Ordered item ZONEORDERED',
  '- [ ] Task item ZONETASK',
  '> Quote paragraph ZONEQUOTE',
  '| Head A | Head B |\n| --- | --- |\n| Cell ZONECELL | Cell two |',
  '```\ncode ZONECODE\n```',
  'Closing paragraph ZONELAST',
].join('\n\n')

/** Appends a pending insertion of `authorId` to the paragraph holding
 * `needle`. */
function appendPendingInsertion(
  content: JSONContent,
  needle: string,
  text: string,
  authorId: string,
  suggestionId: string,
): void {
  const visit = (node: JSONContent): boolean => {
    if (node.type === 'paragraph' && (node.content ?? []).some((child) => child.text?.includes(needle))) {
      node.content = [...(node.content ?? []), {
        marks: [{
          attrs: {
            authorId,
            createdAt: 1_784_112_000,
            id: suggestionId,
            kind: 'insertion',
            patchId: `${suggestionId.slice(0, -1)}f`,
            suggestionId,
          },
          type: 'insertion',
        }],
        text,
        type: 'text',
      }]
      return true
    }
    return (node.content ?? []).some(visit)
  }
  if (!visit(content)) throw new Error(`The catalog seed has no paragraph with ${needle}`)
}

/** The catalog document: every block kind, a pending insertion of another
 * person and one of the writer's own. */
function catalogDocument(): JSONContent {
  const content = parseEditorMarkdown(CATALOG_MARKDOWN)
  appendPendingInsertion(content, 'ZONEFOREIGN', ' FOREIGN', WRITER_B.id, 'aaaaaaaa-0000-4000-8000-000000000001')
  appendPendingInsertion(content, 'ZONEOWN', ' OWN', WRITER_A.id, 'bbbbbbbb-0000-4000-8000-000000000001')
  return content
}

type Step = { label: string; run: (writer: WriterPage) => Promise<void> }

/** Puts the editor's caret into the text block holding `needle`; the keys
 * that follow are real. */
function caret(needle: string, where: 'after' | 'before' | 'end' | 'start'): Step {
  return {
    label: `caret ${where} ${needle}`,
    run: async (writer) => {
      await writer.editor.evaluate((root, target) => {
        const editor = (root as HTMLElement & { editor?: CatalogEditor }).editor
        if (!editor) throw new Error('The editor surface exposes no editor')
        let position: number | null = null
        editor.state.doc.descendants((node, offset) => {
          if (position !== null) return false
          if (!node.isTextblock || !node.textContent.includes(target.needle)) return true
          if (target.where === 'start') position = offset + 1
          else if (target.where === 'end') position = offset + 1 + node.content.size
          else {
            node.descendants((child, childOffset) => {
              if (position !== null || !child.isText) return position === null
              const index = child.text!.indexOf(target.needle)
              if (index < 0) return true
              position = offset + 1 + childOffset + index
                + (target.where === 'after' ? target.needle.length : 0)
              return false
            })
          }
          return false
        })
        if (position === null) throw new Error(`No text block holds ${target.needle}`)
        editor.chain().focus().setTextSelection(position).run()
      }, { needle, where })
      await expectEditorSelectionFollowsPage(writer)
    },
  }
}

/** Selects from the start of `from` to the end of `to`. */
function select(from: string, to = from): Step {
  return {
    label: `select ${from}..${to}`,
    run: async (writer) => {
      await writer.editor.evaluate((root, range) => {
        const editor = (root as HTMLElement & { editor?: CatalogEditor }).editor
        if (!editor) throw new Error('The editor surface exposes no editor')
        const find = (needle: string, end: boolean): number => {
          let found: number | null = null
          editor.state.doc.descendants((node, offset) => {
            if (found !== null) return false
            if (!node.isText) return true
            const index = node.text!.indexOf(needle)
            if (index >= 0) found = offset + index + (end ? needle.length : 0)
            return found === null
          })
          if (found === null) throw new Error(`No text holds ${needle}`)
          return found
        }
        editor.chain().focus().setTextSelection({
          from: find(range.from, false),
          to: find(range.to, true),
        }).run()
      }, { from, to })
      await expectEditorSelectionFollowsPage(writer)
    },
  }
}

function key(name: string, times = 1): Step {
  return {
    label: times > 1 ? `${name} x${times}` : name,
    run: async (writer) => {
      for (let index = 0; index < times; index += 1) {
        await writer.page.keyboard.press(name)
        await writer.page.waitForTimeout(80)
      }
    },
  }
}

function type(text: string): Step {
  return {
    label: `type ${JSON.stringify(text)}`,
    run: async (writer) => {
      for (const character of text) {
        await writer.page.keyboard.type(character)
        await writer.page.waitForTimeout(60)
      }
    },
  }
}

/** Runs the editor's own paste handling with clipboard content, without the
 * system clipboard (which belongs to the person running the tests). */
function paste(text: string, html?: string): Step {
  return {
    label: `paste ${JSON.stringify(html ?? text)}`,
    run: async (writer) => {
      await writer.editor.evaluate((root, content) => {
        const view = (root as HTMLElement & { editor?: CatalogEditor }).editor?.view
        if (!view) throw new Error('The editor surface exposes no editor view')
        if (content.html) view.pasteHTML(content.html)
        else view.pasteText(content.text)
      }, { html, text })
      await writer.page.waitForTimeout(80)
    },
  }
}

/** The part of the editor the catalog steps use. */
type CatalogNode = {
  content: { size: number }
  descendants: (visit: (node: CatalogNode, offset: number) => boolean | void) => void
  isText: boolean
  isTextblock: boolean
  text?: string
  textContent: string
}
type CatalogEditor = {
  chain: () => {
    focus: () => { setTextSelection: (position: number | { from: number; to: number }) => { run: () => boolean } }
  }
  state: { doc: CatalogNode }
  view: { pasteHTML: (html: string) => boolean; pasteText: (text: string) => boolean }
}

type CatalogAction = {
  description: string
  /** How the stored document's final text changed, block by block:
   * `-` removed, `+` added (blocks as the Markdown projection writes them). */
  changed: string[]
  /** Names the action's own copy of the catalog document. */
  id: string
  /** The reason the editor names last, or null when it refused nothing. */
  refusal: RefusalReason | null
  steps: Step[]
}

/** The blocks of the final projection that the action changed. */
function blockDiff(before: string, after: string): string[] {
  const left = before.split('\n\n')
  const right = after.split('\n\n')
  let start = 0
  while (start < left.length && start < right.length && left[start] === right[start]) start += 1
  let end = 0
  while (
    end < left.length - start
    && end < right.length - start
    && left[left.length - 1 - end] === right[right.length - 1 - end]
  ) end += 1
  return [
    ...left.slice(start, left.length - end).map((block) => `-${block}`),
    ...right.slice(start, right.length - end).map((block) => `+${block}`),
  ]
}

function sentUpdates(writer: WriterPage): number {
  return writer.transports.reduce((count, transport) => count + transport.updates().length, 0)
}

/**
 * Runs each action on its own copy of the catalog document in a fresh page
 * of a suggest-access writer and checks the contract: the refusal the
 * editor names (in German, as a status line), what the store holds
 * (identical to the writer's own document, original text untouched), no
 * refusal by the service, and a session that stays connected and writable.
 */
async function runCatalog(
  browser: Parameters<Parameters<typeof test>[2]>[0]['browser'],
  actions: readonly CatalogAction[],
): Promise<void> {
  const stack = await CollaborationStack.start({
    documents: actions.map((action) => ({
      content: catalogDocument(),
      id: `catalog-${action.id}`,
    })),
  })
  try {
    for (const action of actions) {
      const documentId = `catalog-${action.id}`
      const writer = await openWriter(browser, stack, WRITER_A, {
        access: 'suggest',
        documentId,
        seedText: 'First paragraph',
      })
      try {
        await test.step(action.description, async () => {
          const context = action.description
          const finalBefore = stack.storeText(documentId, 'final')
          const originalBefore = stack.storeText(documentId, 'original')
          const rejectionsBefore = stack.rejections()
          const sentBefore = sentUpdates(writer)
          let lastRefusal: string | null = null
          for (const step of action.steps) {
            await step.run(writer)
            const line = writer.page.locator('[data-collaboration-edit-refusal]')
            if (await line.count() > 0) {
              await expect(line, context).toHaveAttribute('role', 'status')
              lastRefusal = await line.textContent()
            }
          }
          await writer.page.waitForTimeout(500)
          const sent = writer.transports.flatMap((transport) => transport.updates()).slice(sentBefore)
          const describeSent = `${context}; sent: ${sent.map((update) => describeUpdate(update.payload!)).join(' ')}`
          if (sent.length > 0) {
            expect.soft(await settledDurability(writer), describeSent).toBe('saved')
          }
          await expect.soft(writer.state, context).toHaveAttribute('data-has-unconfirmed', 'false')
          expect.soft(stack.rejections(), describeSent).toEqual(rejectionsBefore)
          expect.soft(lastRefusal, describeSent).toBe(action.refusal === null ? null : REFUSAL_TEXT[action.refusal])
          await expect.soft(writer.state, context).toHaveAttribute('data-connection-status', 'connected')
          await expect.soft(writer.state, context).toHaveAttribute('data-can-edit', 'true')
          await expect.soft(writer.editor, context).toHaveAttribute('contenteditable', 'true')
          const live = await writerDocument(writer)
          const stored = stack.storeDocument(documentId)
          try {
            expect.soft(projectionText(stored, 'final'), describeSent).toBe(projectionText(live, 'final'))
            expect.soft(projectionText(stored, 'original'), describeSent).toBe(projectionText(live, 'original'))
            expect.soft(suggestionsOf(stored), describeSent).toEqual(suggestionsOf(live))
          } finally {
            live.destroy()
            stored.destroy()
          }
          expect.soft(stack.storeText(documentId, 'original'), describeSent).toBe(originalBefore)
          expect.soft(blockDiff(finalBefore, stack.storeText(documentId, 'final')), describeSent)
            .toEqual(action.changed)
          expect.soft(writer.consoleErrors, context).toEqual([])
        })
      } finally {
        await closeWriters([writer])
      }
    }
  } finally {
    await stack.stop()
  }
}

const FIRST = 'First paragraph ZONEONE ends here'
const SECOND = 'Second paragraph ZONETWO ends here'
const LIST = '- List item ZONELIST\n- List item with a nested list\n  - Nested item ZONENESTED'
const TABLE = '\n| Head A        | Head B   |\n| ------------- | -------- |\n| Cell ZONECELL | Cell two |'
const CLOSING = 'Closing paragraph ZONELAST'

test('suggest mode with real keys: every new-block action is either stored by the service or refused in the editor with a German reason', async ({ browser }) => {
  test.setTimeout(240_000)
  await runCatalog(browser, [
    { id: 'enter-paragraph-end', description: 'Enter at the end of a paragraph, then typing', steps: [caret('ZONEONE', 'end'), key('Enter'), type('Added')], refusal: null, changed: ['+Added'] },
    { id: 'enter-paragraph-middle', description: 'Enter in the middle of a paragraph', steps: [caret('ZONEONE', 'after'), key('Enter')], refusal: 'split_moves_content', changed: [] },
    { id: 'enter-paragraph-start', description: 'Enter at the start of a paragraph', steps: [caret('ZONETWO', 'start'), key('Enter')], refusal: 'split_moves_content', changed: [] },
    { id: 'enter-heading-end', description: 'Enter at the end of a heading, then typing', steps: [caret('ZONEHEAD', 'end'), key('Enter'), type('Added')], refusal: null, changed: ['+Added'] },
    { id: 'enter-list-item-end', description: 'Enter at the end of a list item, then typing', steps: [caret('ZONELIST', 'end'), key('Enter'), type('Added')], refusal: null, changed: [`-${LIST}`, '+- List item ZONELIST\n- Added\n- List item with a nested list\n  - Nested item ZONENESTED'] },
    { id: 'enter-list-item-with-nested-list', description: 'Enter at the end of a list item that holds a nested list', steps: [caret('with a nested list', 'end'), key('Enter')], refusal: 'split_moves_content', changed: [] },
    { id: 'enter-twice-list-item-end', description: 'Enter twice at the end of a list item', steps: [caret('ZONELIST', 'end'), key('Enter'), key('Enter')], refusal: null, changed: [`-${LIST}`, '+- List item ZONELIST\n- \n- \n- List item with a nested list\n  - Nested item ZONENESTED'] },
    { id: 'enter-quote-end', description: 'Enter at the end of a quote, then typing', steps: [caret('ZONEQUOTE', 'end'), key('Enter'), type('Added')], refusal: null, changed: ['-> Quote paragraph ZONEQUOTE', '+> Quote paragraph ZONEQUOTE\n>\n> Added'] },
    { id: 'enter-table-cell-end', description: 'Enter at the end of a table cell, then typing', steps: [caret('ZONECELL', 'end'), key('Enter'), type('Added')], refusal: null, changed: [`-${TABLE}`, '+\n| Head A                 | Head B   |\n| ---------------------- | -------- |\n| Cell ZONECELL<br>Added | Cell two |'] },
    { id: 'enter-code-block', description: 'Enter inside a code block, then typing', steps: [caret('ZONECODE', 'end'), key('Enter'), type('Added')], refusal: null, changed: ['-\n```\ncode ZONECODE\n```', '+\n```\ncode ZONECODE\nAdded\n```'] },
    { id: 'enter-task-item-end', description: 'Enter at the end of a task item, then typing', steps: [caret('ZONETASK', 'end'), key('Enter'), type('Added')], refusal: null, changed: ['-- [ ] Task item ZONETASK', '+- [ ] Task item ZONETASK\n- [ ] Added'] },
    { id: 'hard-break', description: 'Shift+Enter at the end of a paragraph', steps: [caret('ZONEONE', 'end'), key('Shift+Enter')], refusal: 'node_marks_unsupported', changed: [] },
    { id: 'enter-twice-paragraph-end', description: 'Enter twice at the end of a paragraph', steps: [caret('ZONEONE', 'end'), key('Enter'), key('Enter')], refusal: null, changed: ['+', '+&nbsp;'] },
    { id: 'type-enter-type', description: 'typing, Enter, typing', steps: [caret('ZONEONE', 'end'), type(' More'), key('Enter'), type('Added')], refusal: null, changed: [`-${FIRST}`, `+${FIRST} More`, '+Added'] },
    { id: 'enter-after-own-suggestion', description: 'Enter at the end of a paragraph that ends in an own suggestion', steps: [caret('ZONEOWN', 'end'), key('Enter')], refusal: null, changed: ['+'] },
    { id: 'enter-after-foreign-suggestion', description: "Enter at the end of a paragraph that ends in another person's suggestion", steps: [caret('ZONEFOREIGN', 'end'), key('Enter')], refusal: 'foreign_suggestion_changed', changed: [] },
    { id: 'type-after-foreign-suggestion', description: "typing directly behind another person's suggestion", steps: [caret('ZONEFOREIGN', 'end'), type('X')], refusal: 'foreign_suggestion_changed', changed: [] },
    { id: 'enter-inside-own-paragraph', description: 'Enter inside the own new paragraph', steps: [caret('ZONEONE', 'end'), key('Enter'), type('Added'), key('ArrowLeft', 2), key('Enter')], refusal: null, changed: ['+Add', '+ed'] },
    { id: 'enter-ordered-item-end', description: 'Enter at the end of an ordered item, then typing', steps: [caret('ZONEORDERED', 'end'), key('Enter'), type('Added')], refusal: null, changed: ['-1. Ordered item ZONEORDERED', '+1. Ordered item ZONEORDERED\n2. Added'] },
  ])
})

test('suggest mode with real keys: every deletion action is either stored by the service or refused in the editor with a German reason', async ({ browser }) => {
  test.setTimeout(240_000)
  await runCatalog(browser, [
    { id: 'backspace-paragraph-start', description: 'Backspace at the start of a paragraph', steps: [caret('ZONETWO', 'start'), key('Backspace')], refusal: null, changed: [`-${FIRST}`, `-${SECOND}`, `+${FIRST}${SECOND}`] },
    { id: 'delete-paragraph-end', description: 'Delete at the end of a paragraph', steps: [caret('ZONEONE', 'end'), key('Delete')], refusal: null, changed: [`-${FIRST}`, `-${SECOND}`, `+${FIRST}${SECOND}`] },
    { id: 'backspace-list-item-start', description: 'Backspace at the start of a list item', steps: [caret('with a nested list', 'start'), key('Backspace')], refusal: 'node_marks_unsupported', changed: [] },
    { id: 'backspaces-inside-sentence', description: 'five Backspaces inside a sentence', steps: [caret('ZONEONE', 'after'), key('Backspace', 5)], refusal: null, changed: [`-${FIRST}`, '+First paragraph ZO ends here'] },
    { id: 'shift-backspaces-inside-sentence', description: 'five Backspaces with Shift held inside a sentence', steps: [caret('ZONEONE', 'after'), key('Shift+Backspace', 5)], refusal: null, changed: [`-${FIRST}`, '+First paragraph ZO ends here'] },
    { id: 'deletes-inside-sentence', description: 'five Deletes inside a sentence', steps: [caret('ZONEONE', 'before'), key('Delete', 5)], refusal: null, changed: [`-${FIRST}`, '+First paragraph NE ends here'] },
    { id: 'backspaces-paragraph-end', description: 'five Backspaces at the end of a paragraph', steps: [caret('ZONEONE', 'end'), key('Backspace', 5)], refusal: null, changed: [`-${FIRST}`, '+First paragraph ZONEONE ends'] },
    { id: 'backspace-own-text-away', description: 'typing, then Backspace until the own text is gone', steps: [caret('ZONEONE', 'end'), type(' More'), key('Backspace', 5)], refusal: 'suggestion_would_disappear', changed: [`-${FIRST}`, `+${FIRST} `] },
    { id: 'backspace-own-text-partly', description: 'typing, then two Backspaces', steps: [caret('ZONEONE', 'end'), type(' More'), key('Backspace', 2)], refusal: null, changed: [`-${FIRST}`, `+${FIRST} Mo`] },
    { id: 'backspace-through-own-suggestion', description: 'Backspace through an own pending suggestion', steps: [caret('ZONEOWN', 'end'), key('Backspace', 5)], refusal: 'suggestion_would_disappear', changed: ['-Paragraph with an own suggestion ZONEOWN OWN', '+Paragraph with an own suggestion ZONEOWN '] },
    { id: 'backspace-selected-word', description: 'a selected word, Backspace', steps: [select('ZONETWO'), key('Backspace')], refusal: null, changed: [`-${SECOND}`, '+Second paragraph ends here'] },
    { id: 'backspace-across-paragraphs', description: 'a selection across two paragraphs, Backspace', steps: [select('ends here', 'Second'), key('Backspace')], refusal: null, changed: [`-${FIRST}`, `-${SECOND}`, '+First paragraph ZONEONE paragraph ZONETWO ends here'] },
    { id: 'backspace-before-own-text', description: 'typing, the caret before the own text, Backspace', steps: [caret('ZONEONE', 'end'), type(' More'), key('ArrowLeft', 5), key('Backspace')], refusal: 'adjacent_own_suggestion_conflict', changed: [`-${FIRST}`, `+${FIRST} More`] },
    { id: 'backspace-joining-lists', description: 'Backspace joining an ordered list to the list before it', steps: [caret('ZONEORDERED', 'start'), key('Backspace')], refusal: 'node_marks_unsupported', changed: [] },
    { id: 'backspace-next-to-own-join', description: 'Backspace at the start of a paragraph, then again next to the join', steps: [caret('ZONETWO', 'start'), key('Backspace'), key('Backspace')], refusal: 'adjacent_own_suggestion_conflict', changed: [`-${FIRST}`, `-${SECOND}`, `+${FIRST}${SECOND}`] },
    { id: 'delete-before-nested-list', description: 'Delete before a nested list', steps: [caret('with a nested list', 'end'), key('Delete')], refusal: 'unsupported_structure', changed: [] },
  ])
})

test('suggest mode with real keys: every formatting and block action is either stored by the service or refused in the editor with a German reason', async ({ browser }) => {
  test.setTimeout(240_000)
  await runCatalog(browser, [
    { id: 'bold-original-word', description: 'a selected original word, bold', steps: [select('ZONETWO'), key('ControlOrMeta+b')], refusal: null, changed: [`-${SECOND}`, '+Second paragraph **ZONETWO** ends here'] },
    { id: 'bold-own-text', description: 'typing, selecting it, bold', steps: [caret('ZONEONE', 'end'), type(' More'), key('Shift+ArrowLeft', 4), key('ControlOrMeta+b')], refusal: null, changed: [`-${FIRST}`, `+${FIRST} **More**`] },
    { id: 'italic-across-own-suggestion', description: 'original text and an own suggestion selected, italic', steps: [caret('ZONEOWN', 'end'), key('Shift+ArrowLeft', 12), key('ControlOrMeta+i')], refusal: 'format_overlaps_own_suggestion', changed: [] },
    { id: 'unbold-word', description: 'bold removed from a bold word', steps: [select('bold'), key('ControlOrMeta+b')], refusal: null, changed: ['-Paragraph with a **bold** word ZONEBOLD', '+Paragraph with a bold word ZONEBOLD'] },
    { id: 'bold-across-own-join', description: 'two paragraphs joined, then bold across the join', steps: [caret('ZONETWO', 'start'), key('Backspace'), select('here', 'Second'), key('ControlOrMeta+b')], refusal: 'format_overlaps_own_suggestion', changed: [`-${FIRST}`, `-${SECOND}`, `+${FIRST}${SECOND}`] },
    { id: 'tab-in-list-item', description: 'Tab in a list item', steps: [caret('with a nested list', 'end'), key('Tab')], refusal: 'node_marks_unsupported', changed: [] },
    { id: 'shift-tab-nested-item', description: 'Shift+Tab in a nested list item', steps: [caret('ZONENESTED', 'end'), key('Shift+Tab')], refusal: 'node_marks_unsupported', changed: [] },
    { id: 'heading-input-rule', description: 'a heading typed as "# " at the start of a paragraph', steps: [caret('Closing', 'start'), type('# ')], refusal: 'node_marks_unsupported', changed: [`-${CLOSING}`, `+#${CLOSING}`] },
    { id: 'list-input-rule', description: 'a list typed as "- " at the start of a paragraph', steps: [caret('Closing', 'start'), type('- ')], refusal: 'node_marks_unsupported', changed: [`-${CLOSING}`, `+-${CLOSING}`] },
    { id: 'bold-input-rule', description: 'bold typed as "**strong** " at the end of a paragraph', steps: [caret('ZONEONE', 'end'), type(' **strong** ')], refusal: null, changed: [`-${FIRST}`, `+${FIRST} **strong** `] },
    { id: 'heading-shortcut', description: 'the heading shortcut', steps: [caret('ZONELAST', 'end'), key('ControlOrMeta+Alt+2')], refusal: 'node_marks_unsupported', changed: [] },
    { id: 'bullet-list-shortcut', description: 'the bullet list shortcut', steps: [caret('ZONELAST', 'end'), key('ControlOrMeta+Shift+8')], refusal: 'node_marks_unsupported', changed: [] },
  ])
})

test('suggest mode with real keys: every paste, whole-document and automatic-formatting action is either stored by the service or refused in the editor with a German reason', async ({ browser }) => {
  test.setTimeout(240_000)
  await runCatalog(browser, [
    { id: 'paste-lines-paragraph-end', description: 'two pasted lines at the end of a paragraph', steps: [caret('ZONELAST', 'end'), paste(' One\nTwo')], refusal: null, changed: [`-${CLOSING}`, `+${CLOSING} One`, '+Two'] },
    { id: 'paste-lines-paragraph-middle', description: 'two pasted lines in the middle of a paragraph', steps: [caret('ZONELAST', 'before'), paste('One\nTwo')], refusal: 'split_moves_content', changed: [] },
    { id: 'paste-word', description: 'a pasted word in the middle of a paragraph', steps: [caret('ZONELAST', 'before'), paste('Word ')], refusal: null, changed: [`-${CLOSING}`, '+Closing paragraph Word ZONELAST'] },
    { id: 'paste-list', description: 'a pasted list at the end of a paragraph', steps: [caret('ZONELAST', 'end'), paste('A\nB', '<ul><li><p>A</p></li><li><p>B</p></li></ul>')], refusal: 'node_marks_unsupported', changed: [] },
    { id: 'paste-domain-after-original', description: 'a pasted domain ending and a second line directly after an original word', steps: [caret('ZONELAST', 'end'), paste('.com\nTwo')], refusal: null, changed: [`-${CLOSING}`, `+${CLOSING}.com`, '+Two'] },
    { id: 'select-all-type', description: 'everything selected, then typing', steps: [caret('ZONEONE', 'end'), key('ControlOrMeta+a'), type('x')], refusal: 'foreign_collision', changed: [] },
    { id: 'domain-after-original-word', description: 'a domain ending typed directly after an original word', steps: [caret('ZONEONE', 'after'), type('.com next')], refusal: null, changed: [`-${FIRST}`, '+First paragraph ZONEONE.com next ends here'] },
    { id: 'domain-own-words', description: 'a domain typed as own words', steps: [caret('ZONEONE', 'end'), type(' example.com next')], refusal: null, changed: [`-${FIRST}`, `+${FIRST} example.com next`] },
  ])
})

test('a suggest-mode Enter at the end of a paragraph, a list item and a task item proposes the new block, and the other writer sees it', async ({ browser }) => {
  test.setTimeout(120_000)
  const documentId = 'enter-at-end-doc'
  const markdown = 'Opening paragraph\n\n- First item\n\n- [ ] First task'
  const stack = await CollaborationStack.start({ documents: [{ id: documentId, markdown }] })
  const writers: WriterPage[] = []
  try {
    const suggester = await openWriter(browser, stack, WRITER_A, {
      access: 'suggest',
      documentId,
      seedText: 'Opening paragraph',
    })
    writers.push(suggester)
    const editor = await openWriter(browser, stack, WRITER_B, { documentId, seedText: 'Opening paragraph' })
    writers.push(editor)

    // Block indexes as the editor shows them after each proposal: the new
    // paragraph comes second, the list keeps its new item inside.
    for (const [block, text] of [[0, 'MARKE1'], [2, 'MARKE2'], [3, 'MARKE3']] as const) {
      await placeCaretAtBlockEnd(suggester, block)
      await suggester.page.keyboard.press('Enter')
      await typeSlowly(suggester, text, 60)
      await expectSaved(suggester)
    }

    expect(stack.rejections()).toEqual({})
    await expect(suggester.page.locator('[data-collaboration-edit-refusal]')).toHaveCount(0)
    // Read as a user would: each proposal is a new block of the same kind
    // behind the block the writer ended, and the original is untouched.
    const proposed = 'Opening paragraph\n\nMARKE1\n\n- First item\n- MARKE2\n\n- [ ] First task\n- [ ] MARKE3'
    expect(stack.storeText(documentId, 'final')).toBe(proposed)
    expect(stack.storeText(documentId, 'original')).toBe('Opening paragraph\n\n- First item\n\n- [ ] First task')
    const stored = stack.storeDocument(documentId)
    try {
      expect(suggestionsOf(stored).every((suggestion) => suggestion.endsWith(`:${WRITER_A.id}`))).toBe(true)
      expect(suggestionsOf(stored).length).toBeGreaterThan(0)
    } finally {
      stored.destroy()
    }
    for (const text of ['MARKE1', 'MARKE2', 'MARKE3']) {
      await expect.poll(() => editorText(editor)).toContain(text)
    }
    await expect(suggester.state).toHaveAttribute('data-can-edit', 'true')
    for (const writer of writers) expect(writer.consoleErrors).toEqual([])
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

test('a suggest-mode writer who opens a document that ends in a list or a heading sends nothing on opening and keeps suggesting', async ({ browser }) => {
  test.setTimeout(120_000)
  const documents = [
    { id: 'ends-in-list-doc', markdown: 'Opening paragraph\n\n- Last item' },
    { id: 'ends-in-heading-doc', markdown: 'Opening paragraph\n\n## Closing heading' },
  ]
  const stack = await CollaborationStack.start({ documents })
  try {
    for (const document of documents) {
      for (const access of ['edit', 'suggest'] as const) {
        const writer = await openWriter(browser, stack, WRITER_A, {
          access,
          documentId: document.id,
          seedText: 'Opening paragraph',
        })
        try {
          // Opening writes nothing, whatever the access.
          await writer.page.waitForTimeout(500)
          expect(sentUpdates(writer), `${document.id} ${access}`).toBe(0)
          expect(stack.storedUpdateCount(document.id)).toBe(0)
          if (access === 'suggest') {
            await placeCaretAtBlockEnd(writer, 0)
            await typeSlowly(writer, 'MARKE1', 60)
            await expectSaved(writer)
            expect(stack.rejections()).toEqual({})
            expect(stack.storeText(document.id, 'final')).toBe(document.markdown.replace('Opening paragraph', 'Opening paragraphMARKE1'))
            expect(stack.storeText(document.id, 'original')).toBe(document.markdown)
            await expect(writer.state).toHaveAttribute('data-can-edit', 'true')
          }
          expect(writer.consoleErrors).toEqual([])
        } finally {
          await closeWriters([writer])
        }
      }
    }
  } finally {
    await stack.stop()
  }
})

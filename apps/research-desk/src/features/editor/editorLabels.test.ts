import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

import {
  editorConflictCopyNotice,
  editorConflictCopyTitle,
  editorCopy,
  taskCheckboxLabelFor,
} from './editorCopy'
import { createEditorExtensions } from './tiptap'

const editorDirectory = fileURLToPath(new URL('.', import.meta.url))

function editorSources(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name)
    if (entry.isDirectory()) return entry.name === 'browser-tests' ? [] : editorSources(path)
    return entry.name.endsWith('.tsx') && !entry.name.endsWith('.test.tsx') ? [path] : []
  })
}

function localizedTaskItem(locale: 'de' | 'en') {
  return createEditorExtensions({
    taskCheckboxLabel: taskCheckboxLabelFor(editorCopy[locale]),
  }).find((extension) => extension.name === 'taskItem')
}

function checkboxLabel(locale: 'de' | 'en', taskText: string): string {
  return localizedTaskItem(locale)?.options.a11y?.checkboxLabel?.({ textContent: taskText }, false)
}

describe('editor labels', () => {
  it('names a task item checkbox in the reader\'s language', () => {
    expect(checkboxLabel('de', 'Milch kaufen')).toBe('Aufgaben-Checkbox für Milch kaufen')
    expect(checkboxLabel('de', '')).toBe('leere Aufgabe')
    expect(checkboxLabel('en', 'Buy milk')).toBe('Task item checkbox for Buy milk')
    expect(checkboxLabel('de', 'Budget $& klären')).toBe('Aufgaben-Checkbox für Budget $& klären')
  })

  it('names a conflict copy after its document and the moment, in the reader\'s language', () => {
    const at = new Date(2026, 2, 4, 5, 6, 7)
    expect(editorConflictCopyTitle('Projektplan.md', at, 'de'))
      .toBe('Projektplan (Konfliktkopie 04.03.2026 05:06:07).md')
    expect(editorConflictCopyTitle('Projektplan.md', at, 'en'))
      .toBe('Projektplan (conflicted copy Mar 4, 2026 5:06:07 AM).md')
    // A title keeps what was typed into it, a `$&` included.
    expect(editorConflictCopyTitle('Budget $& Plan.md', at, 'de'))
      .toBe('Budget $& Plan (Konfliktkopie 04.03.2026 05:06:07).md')
  })

  it('says at the original and at the copy where each version is, in the reader\'s language', () => {
    const copyTitle = 'Plan (Konfliktkopie 04.03.2026 05:06:07).md'
    expect(editorConflictCopyNotice('de', 'concurrent_change', 'original', copyTitle)).toBe(
      'Ihre Fassung ließ sich nicht mit einer inzwischen gespeicherten Änderung zusammenführen. '
      + 'Das Dokument hat die gespeicherte Fassung übernommen, Ihre liegt in der Kopie '
      + '„Plan (Konfliktkopie 04.03.2026 05:06:07).md“.',
    )
    expect(editorConflictCopyNotice('de', 'concurrent_change', 'copy', 'Plan.md')).toBe(
      'Diese Kopie enthält Ihre Fassung von „Plan.md“, die sich nicht mit einer dort inzwischen '
      + 'gespeicherten Änderung zusammenführen ließ.',
    )
    expect(editorConflictCopyNotice('de', 'content_mode_changed', 'original', copyTitle)).toBe(
      'Das Dokument wurde inzwischen in den Modus Zusammenarbeit umgestellt. Ihre Fassung von vor der '
      + 'Umstellung liegt in der Kopie „Plan (Konfliktkopie 04.03.2026 05:06:07).md“.',
    )
    expect(editorConflictCopyNotice('de', 'content_mode_changed', 'copy', 'Plan.md')).toBe(
      'Diese Kopie enthält Ihre Fassung von „Plan.md“ von vor der Umstellung in den Modus Zusammenarbeit.',
    )
    expect(editorConflictCopyNotice('en', 'concurrent_change', 'original', 'Plan (conflicted copy).md')).toBe(
      'Your version could not be merged with a change saved in the meantime. The document took the saved '
      + 'version; yours is in the copy “Plan (conflicted copy).md”.',
    )
    expect(editorConflictCopyNotice('en', 'content_mode_changed', 'copy', 'Plan.md')).toBe(
      'This copy holds your version of “Plan.md” from before it was switched to collaboration mode.',
    )
    // A title keeps what was typed into it, a `$&` included.
    expect(editorConflictCopyNotice('de', 'concurrent_change', 'copy', 'Budget $& Plan.md'))
      .toContain('„Budget $& Plan.md“')
  })

  it('keeps the schema options of the task item next to its label', () => {
    expect(localizedTaskItem('de')?.options).toMatchObject({
      HTMLAttributes: {},
      nested: true,
      taskListTypeName: 'taskList',
    })
  })

  it('hands the editor surface\'s copy to the task checkbox label', () => {
    const surface = readFileSync(join(editorDirectory, 'core/MarkdownEditorSurface.tsx'), 'utf8')
    expect(surface).toContain('taskCheckboxLabel: taskCheckboxLabelFor(copy),')
  })

  it('keeps literal aria-label, label and title attributes out of the editor components', () => {
    const literals = editorSources(editorDirectory).flatMap((path) => (
      [...readFileSync(path, 'utf8').matchAll(/\b(?:aria-label|label|title)="[^"]*[A-Za-z][^"]*"/g)]
        .map((match) => `${path.slice(editorDirectory.length)}: ${match[0]}`)
    ))
    expect(literals).toEqual([])
  })
})

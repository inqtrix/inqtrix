import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

import { editorCopy, taskCheckboxLabelFor } from './editorCopy'
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

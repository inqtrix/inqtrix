import {
  Code2,
  Heading1,
  Heading2,
  Heading3,
  List,
  ListOrdered,
  ListTodo,
  Minus,
  Quote,
  Table2,
  Type,
  type LucideIcon,
} from '@/components/icons'
import type { BlockActionId } from './blockActions'

/** Localized strings for the `/` menu, assembled from `editorCopy` in
 * `editorCopy.ts` so all copy stays in one place (DE + EN). */
export type SlashLabels = {
  title: string
  empty: string
  navHint: string
  selectHint: string
  closeHint: string
  groupStyle: string
  groupInsert: string
  text: string
  heading1: string
  heading2: string
  heading3: string
  bulletList: string
  orderedList: string
  taskList: string
  blockquote: string
  codeBlock: string
  table: string
  divider: string
  suggestUnavailable: string
}

export type SlashItem = {
  id: BlockActionId
  label: string
  icon: LucideIcon
  group: string
  /** Lowercased search terms (localized label + stable synonyms), so `/h1`,
   * `/todo`, `/table` match regardless of the UI language. */
  keywords: string
  disabled?: boolean
  description?: string
}

/** Full item list in display order, with icons + localized labels. */
export function buildSlashItems(labels: SlashLabels): SlashItem[] {
  const style = labels.groupStyle
  const insert = labels.groupInsert
  return (
    [
      { id: 'paragraph', label: labels.text, icon: Type, group: style, keywords: `${labels.text} text paragraph absatz p` },
      { id: 'heading1', label: labels.heading1, icon: Heading1, group: style, keywords: `${labels.heading1} h1 heading title überschrift` },
      { id: 'heading2', label: labels.heading2, icon: Heading2, group: style, keywords: `${labels.heading2} h2 heading überschrift` },
      { id: 'heading3', label: labels.heading3, icon: Heading3, group: style, keywords: `${labels.heading3} h3 heading überschrift` },
      { id: 'bulletList', label: labels.bulletList, icon: List, group: style, keywords: `${labels.bulletList} bullet ul unordered liste aufzählung` },
      { id: 'orderedList', label: labels.orderedList, icon: ListOrdered, group: style, keywords: `${labels.orderedList} numbered ol ordered nummeriert liste` },
      { id: 'taskList', label: labels.taskList, icon: ListTodo, group: style, keywords: `${labels.taskList} todo task checkbox checklist aufgabe` },
      { id: 'blockquote', label: labels.blockquote, icon: Quote, group: style, keywords: `${labels.blockquote} quote blockquote zitat` },
      { id: 'codeBlock', label: labels.codeBlock, icon: Code2, group: style, keywords: `${labels.codeBlock} code codeblock pre` },
      { id: 'table', label: labels.table, icon: Table2, group: insert, keywords: `${labels.table} table grid tabelle` },
      { id: 'divider', label: labels.divider, icon: Minus, group: insert, keywords: `${labels.divider} divider hr rule separator trenner horizontal` },
    ] satisfies SlashItem[]
  ).map((item) => ({ ...item, keywords: item.keywords.toLowerCase() }))
}

/** Filter by the query typed after `/` (empty query → all). */
export function filterSlashItems(items: SlashItem[], query: string): SlashItem[] {
  const q = query.trim().toLowerCase()
  if (!q) return items
  return items.filter((item) => item.keywords.includes(q) || item.label.toLowerCase().includes(q))
}

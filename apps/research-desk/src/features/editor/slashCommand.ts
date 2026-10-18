import { Extension, type Editor } from '@tiptap/core'
import { ReactRenderer } from '@tiptap/react'
import {
  Suggestion,
  type SuggestionKeyDownProps,
  type SuggestionOptions,
  type SuggestionProps,
} from '@tiptap/suggestion'
import { CommandMenu, type CommandMenuItem } from '@/components/ui/command-menu'
import { runBlockAction } from './blockActions'
import { buildSlashItems, filterSlashItems, type SlashItem, type SlashLabels } from './slashItems'

export type SlashCommandConfig = { labels: SlashLabels }

type SlashOptions = { config: SlashCommandConfig | null }

/**
 * `/` slash command menu for the document editor. Built on the MIT
 * `@tiptap/suggestion` utility; the popup reuses the app's `CommandMenu` (same
 * visual language as the `@`-mention menu). Block-only — no AI items (the AI
 * lives in the assistant panel). Active only when the editor is editable.
 */
export const SlashCommandExtension = Extension.create<SlashOptions>({
  name: 'slashCommand',

  addOptions() {
    return { config: null }
  },

  addProseMirrorPlugins() {
    const config = this.options.config
    if (!config) return []
    const allItems = buildSlashItems(config.labels)
    const options: SuggestionOptions<SlashItem, SlashItem> = {
      editor: this.editor,
      char: '/',
      // Tracked block splits use an invisible boundary marker. Treat it like
      // whitespace so `/` still opens the command menu in a newly proposed
      // empty paragraph.
      allowedPrefixes: [' ', '\u200B'],
      startOfLine: false,
      allow: ({ editor }) => editor.isEditable,
      items: ({ editor, query }) => filterSlashItems(
        slashItemsForEditor(allItems, editor, config.labels),
        query,
      ),
      command: ({ editor, range, props }) => {
        if (props.disabled) return
        runBlockAction(editor, props.id, range)
      },
      render: () => createSlashRenderer(config.labels),
    }
    return [Suggestion<SlashItem, SlashItem>(options)]
  },
})

function createSlashRenderer(labels: SlashLabels) {
  let renderer: ReactRenderer | null = null
  let items: SlashItem[] = []
  let active = 0
  let dismissed = false
  let command: ((item: SlashItem) => void) | null = null

  const toMenuItems = (list: SlashItem[]): CommandMenuItem[] =>
    list.map((item) => ({
      description: item.description,
      disabled: item.disabled,
      group: item.group,
      icon: item.icon,
      id: item.id,
      label: item.label,
    }))

  const firstEnabled = () => items.findIndex((item) => !item.disabled)

  const move = (direction: -1 | 1) => {
    if (!items.some((item) => !item.disabled)) return
    let candidate = active
    for (let steps = 0; steps < items.length; steps += 1) {
      candidate = (candidate + direction + items.length) % items.length
      if (!items[candidate]?.disabled) {
        active = candidate
        renderer?.updateProps(menuProps())
        return
      }
    }
  }

  const select = (index: number) => {
    const item = items[index]
    if (item && !item.disabled && command) command(item)
  }

  const menuProps = () => ({
    title: labels.title,
    items: toMenuItems(items),
    activeIndex: active,
    emptyLabel: labels.empty,
    navHint: labels.navHint,
    selectHint: labels.selectHint,
    closeHint: labels.closeHint,
    onSelect: select,
    onHover: (index: number) => {
      active = index
      renderer?.updateProps(menuProps())
    },
  })

  const position = (rect: DOMRect | null | undefined) => {
    const el = renderer?.element as HTMLElement | undefined
    if (!el || !rect) return
    el.style.position = 'fixed'
    el.style.zIndex = '50'
    const height = el.offsetHeight || 320
    const spaceBelow = window.innerHeight - rect.bottom
    const top = spaceBelow > height + 12 ? rect.bottom + 6 : rect.top - height - 6
    el.style.top = `${Math.max(8, Math.round(top))}px`
    el.style.left = `${Math.round(rect.left)}px`
  }

  return {
    onStart: (props: SuggestionProps<SlashItem, SlashItem>) => {
      items = props.items
      active = Math.max(0, firstEnabled())
      dismissed = false
      command = (item) => props.command(item)
      renderer = new ReactRenderer(CommandMenu, { editor: props.editor, props: menuProps() })
      document.body.appendChild(renderer.element)
      position(props.clientRect?.())
    },
    onUpdate: (props: SuggestionProps<SlashItem, SlashItem>) => {
      items = props.items
      command = (item) => props.command(item)
      if (active >= items.length || items[active]?.disabled) {
        active = Math.max(0, firstEnabled())
      }
      if (dismissed) return
      renderer?.updateProps(menuProps())
      position(props.clientRect?.())
    },
    onKeyDown: (props: SuggestionKeyDownProps) => {
      if (dismissed) return false
      const { event } = props
      if (event.key === 'Escape') {
        dismissed = true
        const el = renderer?.element as HTMLElement | undefined
        if (el) el.style.display = 'none'
        return true
      }
      if (items.length === 0) return false
      if (event.key === 'ArrowDown') {
        move(1)
        return true
      }
      if (event.key === 'ArrowUp') {
        move(-1)
        return true
      }
      if (event.key === 'Enter') {
        select(active)
        return true
      }
      return false
    },
    onExit: () => {
      renderer?.destroy()
      const el = renderer?.element as HTMLElement | undefined
      el?.remove()
      renderer = null
    },
  }
}

export function slashItemsForEditor(
  items: readonly SlashItem[],
  editor: Editor,
  labels: SlashLabels,
): SlashItem[] {
  const reviewStorage = (
    editor.storage as unknown as Record<string, unknown>
  ).collaborationReview as
    | { writeMode?: string }
    | undefined
  if (reviewStorage?.writeMode !== 'suggest') {
    return items.map((item) => ({
      ...item,
      description: undefined,
      disabled: false,
    }))
  }
  return items.map((item) => {
    const disabled = item.id === 'table' || item.id === 'divider'
    return {
      ...item,
      description: disabled ? labels.suggestUnavailable : undefined,
      disabled,
    }
  })
}

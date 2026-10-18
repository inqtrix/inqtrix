import { useState } from 'react'
import { Check, Folder, FolderOpen, Plus, Search, Upload, X } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'
import type { FileAssetRecord, FileGroupRecord, FileLibrarySectionRecord } from '@/features/project/types'
import { TypeTile } from './controls'

function PickRow({
  asset,
  checked,
  indent,
  member,
  onToggle,
}: {
  asset: FileAssetRecord
  checked: boolean
  indent?: boolean
  member: boolean
  onToggle: () => void
}) {
  const { t } = useLocale()
  return (
    <button
      className={cn(
        'flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-left transition-colors',
        indent && 'pl-6',
        member ? 'opacity-55' : 'hover:bg-accent',
      )}
      disabled={member}
      onClick={onToggle}
      type="button"
    >
      <span
        className={cn(
          'grid size-4 shrink-0 place-items-center rounded-[5px] border transition-colors',
          checked || member ? 'border-brand bg-brand text-brand-foreground' : 'border-muted-foreground/40 text-transparent',
        )}
      >
        <Check className="size-3" />
      </span>
      <TypeTile asset={asset} size="sm" />
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          <span className="t-mono text-muted-foreground/70">@files:</span>
          <span className="truncate t-list text-foreground">{asset.label}</span>
        </span>
        <span className="block truncate t-meta-sm text-muted-foreground">{asset.fileName}</span>
      </span>
      {member ? <span className="shrink-0 whitespace-nowrap t-hint font-medium text-muted-foreground">{t.vectorIndex.inIndex}</span> : null}
    </button>
  )
}

export function AddDocsPanel({
  docs,
  groups,
  memberIds,
  onAdd,
  onClose,
  onUpload,
  sections,
}: {
  docs: FileAssetRecord[]
  groups: FileGroupRecord[]
  memberIds: Set<string>
  onAdd: (fileIds: string[]) => void
  onClose: () => void
  onUpload?: () => void
  sections: FileLibrarySectionRecord[]
}) {
  const { t } = useLocale()
  const [selected, setSelected] = useState<Set<string>>(() => new Set())
  const [query, setQuery] = useState('')

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  const addAll = (ids: string[]) =>
    setSelected((prev) => {
      const next = new Set(prev)
      ids.forEach((id) => next.add(id))
      return next
    })

  const q = query.trim().toLowerCase()
  const visible = (asset: FileAssetRecord) => !q || `${asset.label} ${asset.fileName}`.toLowerCase().includes(q)

  return (
    <div className="rounded-lg border border-border bg-card shadow-[0_8px_24px_var(--shadow-soft)]">
      <div className="flex items-center gap-2.5 border-b border-border px-3.5 py-2.5">
        <h3 className="shrink-0 t-section text-foreground">{t.vectorIndex.addDocumentsTitle}</h3>
        <label className="ml-auto flex w-40 items-center gap-2 rounded-md border border-border bg-background px-2 focus-within:ring-2 focus-within:ring-ring sm:w-60">
          <Search className="size-3.5 shrink-0 text-muted-foreground" />
          <input
            className="min-w-0 flex-1 border-0 bg-transparent py-1.5 text-sm text-foreground outline-none placeholder:text-muted-foreground"
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t.vectorIndex.addDocumentsFilter}
            value={query}
          />
        </label>
        <Button aria-label={t.fileLibrary.cancel} className="size-7 shrink-0" onClick={onClose} size="icon" type="button" variant="ghost">
          <X className="size-4" />
        </Button>
      </div>

      <div className="max-h-[320px] overflow-y-auto p-2">
        {sections.map((section) => {
          const sectionDocs = docs.filter((asset) => asset.sectionId === section.id && visible(asset))
          if (sectionDocs.length === 0) return null
          const ungrouped = sectionDocs.filter((asset) => asset.groupId === null)
          const sectionGroups = groups.filter((group) => group.sectionId === section.id)
          return (
            <div className="mb-2" key={section.id}>
              <div className="flex items-center gap-1.5 px-1.5 py-1 t-caption text-muted-foreground">
                <Folder className="size-3" />
                {section.title}
              </div>
              {ungrouped.map((asset) => (
                <PickRow
                  asset={asset}
                  checked={selected.has(asset.id)}
                  key={asset.id}
                  member={memberIds.has(asset.id)}
                  onToggle={() => toggle(asset.id)}
                />
              ))}
              {sectionGroups.map((group) => {
                const groupDocs = sectionDocs.filter((asset) => asset.groupId === group.id)
                if (groupDocs.length === 0) return null
                const addable = groupDocs.filter((asset) => !memberIds.has(asset.id)).map((asset) => asset.id)
                return (
                  <div className="mt-0.5" key={group.id}>
                    <div className="flex items-center gap-1.5 px-1.5 py-0.5 t-meta-sm text-muted-foreground">
                      <FolderOpen className="size-3 text-file" />
                      <span className="truncate">{group.title}</span>
                      {addable.length > 0 ? (
                        <button
                          className="ml-auto shrink-0 whitespace-nowrap rounded border border-border px-1.5 py-0.5 t-hint font-medium text-muted-foreground hover:bg-accent hover:text-foreground"
                          onClick={() => addAll(addable)}
                          type="button"
                        >
                          {t.vectorIndex.wholeGroup}
                        </button>
                      ) : null}
                    </div>
                    {groupDocs.map((asset) => (
                      <PickRow
                        asset={asset}
                        checked={selected.has(asset.id)}
                        indent
                        key={asset.id}
                        member={memberIds.has(asset.id)}
                        onToggle={() => toggle(asset.id)}
                      />
                    ))}
                  </div>
                )
              })}
            </div>
          )
        })}
      </div>

      <div className="flex items-center justify-between gap-3 border-t border-border px-3.5 py-2.5">
        <div className="flex items-center gap-3">
          <span className="t-meta text-muted-foreground">
            {t.vectorIndex.selectedCount.replace('{count}', String(selected.size))}
          </span>
          {onUpload ? (
            <button
              className="inline-flex items-center gap-1.5 t-meta text-muted-foreground transition-colors hover:text-foreground"
              onClick={onUpload}
              type="button"
            >
              <Upload className="size-3.5" />
              {t.vectorIndex.uploadNew}
            </button>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          <Button className="h-8" onClick={onClose} size="sm" type="button" variant="ghost">
            {t.fileLibrary.cancel}
          </Button>
          <Button
            className="h-8 gap-1.5 bg-brand text-brand-foreground hover:bg-brand/90"
            disabled={selected.size === 0}
            onClick={() => onAdd([...selected])}
            size="sm"
            type="button"
          >
            <Plus className="size-3.5" />
            {selected.size > 0 ? t.vectorIndex.addCount.replace('{count}', String(selected.size)) : t.vectorIndex.add}
          </Button>
        </div>
      </div>
    </div>
  )
}

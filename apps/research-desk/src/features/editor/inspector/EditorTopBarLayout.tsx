import type { ReactNode } from 'react'

export function EditorTopBarLayout({
  actions,
  leading,
  primary,
  toolbar,
}: {
  actions: ReactNode
  leading: ReactNode
  primary?: ReactNode
  toolbar: ReactNode
}) {
  return (
    <header
      className="inqtrix-editor-topbar grid min-w-0 inqtrix-panel-header items-center gap-2 overflow-hidden border-b border-border bg-background px-3"
      data-editor-topbar
    >
      <div className="flex min-w-0 items-center gap-2" data-editor-topbar-leading>
        {leading}
      </div>
      <div className="min-w-0 overflow-hidden" data-editor-topbar-toolbar>
        {toolbar}
      </div>
      <div
        className="flex min-w-0 items-center justify-center gap-1"
        data-editor-topbar-primary
      >
        {primary}
      </div>
      <div
        className="flex min-w-0 justify-end gap-0.5"
        data-editor-topbar-actions
      >
        {actions}
      </div>
    </header>
  )
}

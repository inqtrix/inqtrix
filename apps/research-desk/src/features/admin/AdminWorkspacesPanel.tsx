import { useEffect, useMemo, useState } from 'react'

import {
  AlertTriangle,
  Check,
  FolderPlus,
  Plus,
  Search,
  Trash2,
  X,
} from '@/components/icons'
import type { AdminUser, WorkspaceRoleValue } from '@/api/inqtrixClient'
import { InitialsAvatar } from '@/components/ui/avatar'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'
import type { useAdminWorkspaces } from './useAdminWorkspaces'
import { candidateUsers, wouldOrphanLastOwner } from './workspaceModel'

const MEMBER_GRID =
  'grid grid-cols-[minmax(0,1fr)_8.5rem_2.5rem] items-center gap-2'

const ADMIN_SEARCH_INPUT =
  'h-8 w-full rounded-md border border-border bg-background pl-8 pr-3 text-sm text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring'

const TABLE_ROLE_SELECT =
  'border-border bg-background disabled:bg-surface/60 disabled:text-muted-foreground disabled:opacity-100'

const ROLE_ORDER: WorkspaceRoleValue[] = [
  'viewer',
  'commenter',
  'editor',
  'owner',
]

const SEARCH_DEBOUNCE_MS = 200
const MIN_QUERY_LENGTH = 2
const MAX_RESULTS = 8

export function AdminWorkspacesPanel({
  admin,
  users,
}: {
  admin: ReturnType<typeof useAdminWorkspaces>
  users: AdminUser[]
}) {
  const { t } = useLocale()
  const { loadMembers, state } = admin
  const roleLabel = (role: WorkspaceRoleValue) => t.adminWorkspaces.roles[role]

  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [renaming, setRenaming] = useState(false)
  const [renameValue, setRenameValue] = useState('')
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [addQuery, setAddQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [addRole, setAddRole] = useState<WorkspaceRoleValue>('viewer')

  // Debounce the add-member query so a large loaded user list is not
  // re-filtered on every keystroke (the typeahead "feel").
  useEffect(() => {
    const timer = window.setTimeout(
      () => setDebouncedQuery(addQuery),
      SEARCH_DEBOUNCE_MS,
    )
    return () => window.clearTimeout(timer)
  }, [addQuery])

  // Keep a valid selection as the list changes (default to the first).
  useEffect(() => {
    if (state.workspaces.length === 0) {
      if (selectedId !== null) setSelectedId(null)
      return
    }
    if (!state.workspaces.some((workspace) => workspace.workspace_id === selectedId)) {
      setSelectedId(state.workspaces[0].workspace_id)
    }
  }, [selectedId, state.workspaces])

  // Load the selected workspace's members (no-op in demo: pre-seeded).
  // `loadMembers` is a stable callback, so this fires only on selection change.
  useEffect(() => {
    if (selectedId) void loadMembers(selectedId)
  }, [loadMembers, selectedId])

  const selected = state.workspaces.find(
    (workspace) => workspace.workspace_id === selectedId,
  )
  const members = (selectedId && state.members[selectedId]) || []
  const memberUserIds = useMemo(
    () => new Set(members.map((member) => member.user_id)),
    [members],
  )
  const addResults = useMemo(
    () =>
      candidateUsers(users, memberUserIds, debouncedQuery).slice(0, MAX_RESULTS),
    [debouncedQuery, memberUserIds, users],
  )

  if (!state.available) {
    return (
      <p className="t-meta text-muted-foreground">
        {t.adminWorkspaces.notAdmin}
      </p>
    )
  }

  const banner = state.mutationError
    ? state.mutationError === 'last_owner'
      ? t.adminWorkspaces.lastOwner
      : t.adminWorkspaces.mutationFailed
    : null

  async function submitCreate() {
    const name = newName.trim()
    if (!name) return
    const created = await admin.createWorkspace(name)
    setCreating(false)
    setNewName('')
    if (typeof created === 'string') setSelectedId(created)
  }

  async function submitRename() {
    const name = renameValue.trim()
    if (!name || !selected) return
    await admin.renameWorkspace(selected.workspace_id, name)
    setRenaming(false)
  }

  function addMemberFromResult(candidate: AdminUser) {
    if (!selected) return
    void admin.addMember(
      selected.workspace_id,
      {
        display_name: candidate.display_name,
        email: candidate.email,
        user_id: candidate.id,
      },
      addRole,
    )
    setAddQuery('')
  }

  return (
    <div className="flex min-w-0 flex-col gap-4">
      <p className="t-meta max-w-xl text-muted-foreground">
        {t.adminWorkspaces.description}
      </p>

      {banner ? (
        <p className="flex gap-1.5 text-xs font-medium text-destructive" role="alert">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
          <span>{banner}</span>
        </p>
      ) : null}

      <section className="grid min-h-[20rem] min-w-0 overflow-hidden md:grid-cols-[14rem_minmax(0,1fr)]">
        {/* Left rail: the workspace list. */}
        <aside className="flex flex-col gap-1.5 border-b border-border/70 p-3 md:border-b-0 md:border-r">
          <div className="flex items-center justify-between">
            <span className="t-caption text-muted-foreground">
              {t.adminWorkspaces.railTitle}
            </span>
            <Button
              aria-label={t.adminWorkspaces.newWorkspace}
              className="text-muted-foreground"
              onClick={() => {
                setCreating(true)
                setNewName('')
              }}
              size="icon"
              title={t.adminWorkspaces.newWorkspace}
              variant="ghost"
            >
              <Plus className="icon-md" />
            </Button>
          </div>

          {creating ? (
            <form
              className="flex items-center gap-1"
              onSubmit={(event) => {
                event.preventDefault()
                void submitCreate()
              }}
            >
              <Input
                aria-label={t.adminWorkspaces.newWorkspacePlaceholder}
                autoFocus
                className="h-8"
                onChange={(event) => setNewName(event.target.value)}
                placeholder={t.adminWorkspaces.newWorkspacePlaceholder}
                value={newName}
              />
              <Button aria-label={t.adminWorkspaces.create} size="icon" type="submit">
                <Check className="icon-md" />
              </Button>
              <Button
                aria-label={t.adminWorkspaces.cancel}
                onClick={() => setCreating(false)}
                size="icon"
                type="button"
                variant="ghost"
              >
                <X className="icon-md" />
              </Button>
            </form>
          ) : null}

          {state.workspaces.length === 0 && !creating ? (
            <p className="t-meta-sm px-1 text-muted-foreground">
              {t.adminWorkspaces.noWorkspaces}
            </p>
          ) : null}

          <ul className="flex flex-col gap-0.5">
            {state.workspaces.map((workspace) => {
              const active = workspace.workspace_id === selectedId
              return (
                <li key={workspace.workspace_id}>
                  <button
                    className={cn(
                      'flex w-full items-center justify-between gap-2 rounded-md border-l-2 border-transparent px-2 py-1.5 text-left transition-colors',
                      active
                        ? 'border-brand bg-brand-subtle text-brand'
                        : 'text-foreground hover:bg-accent',
                    )}
                    onClick={() => {
                      setSelectedId(workspace.workspace_id)
                      setRenaming(false)
                    }}
                    type="button"
                  >
                    <span className="t-list truncate">{workspace.name}</span>
                    <span className="t-hint shrink-0 tabular-nums text-muted-foreground">
                      {workspace.member_count}
                    </span>
                  </button>
                </li>
              )
            })}
          </ul>
        </aside>

        {/* Right: the selected workspace's members. */}
        <section className="min-w-0">
          {!selected ? (
            <div className="flex h-full flex-col items-center justify-center gap-2 text-center">
              <span className="flex size-9 items-center justify-center rounded-full bg-surface">
                <FolderPlus className="icon-md text-muted-foreground" />
              </span>
              <p className="t-card text-foreground">
                {t.adminWorkspaces.noWorkspaces}
              </p>
              <p className="t-meta max-w-xs text-muted-foreground">
                {t.adminWorkspaces.noWorkspacesHint}
              </p>
            </div>
          ) : (
            <div className="flex min-h-full flex-col">
              {/* Header: name + rename + delete. */}
              <div className="px-4 py-3">
                {renaming ? (
                  <form
                    className="flex flex-1 items-center gap-1"
                    onSubmit={(event) => {
                      event.preventDefault()
                      void submitRename()
                    }}
                  >
                    <Input
                      aria-label={t.adminWorkspaces.rename}
                      autoFocus
                      className="h-8 max-w-xs"
                      onChange={(event) => setRenameValue(event.target.value)}
                      value={renameValue}
                    />
                    <Button aria-label={t.adminWorkspaces.save} size="icon" type="submit">
                      <Check className="icon-md" />
                    </Button>
                    <Button
                      aria-label={t.adminWorkspaces.cancel}
                      onClick={() => setRenaming(false)}
                      size="icon"
                      type="button"
                      variant="ghost"
                    >
                      <X className="icon-md" />
                    </Button>
                  </form>
                ) : (
                  <div className="flex items-center justify-between gap-2">
                    <h3 className="t-section truncate text-foreground">
                      {selected.name}
                    </h3>
                    <div className="flex shrink-0 items-center gap-1">
                      <Button
                        onClick={() => {
                          setRenameValue(selected.name)
                          setRenaming(true)
                        }}
                        size="sm"
                        variant="ghost"
                      >
                        {t.adminWorkspaces.rename}
                      </Button>
                      <Button
                        aria-label={t.adminWorkspaces.deleteWorkspace}
                        className="text-muted-foreground hover:text-destructive"
                        onClick={() => setDeleteOpen(true)}
                        size="icon"
                        title={t.adminWorkspaces.deleteWorkspace}
                        variant="ghost"
                      >
                        <Trash2 className="icon-md" />
                      </Button>
                    </div>
                  </div>
                )}
              </div>

                <div
                  className={cn(
                    MEMBER_GRID,
                    'border-b border-border/70 bg-surface/45 px-4 py-1.5',
                  )}
                >
                <span className="t-caption text-muted-foreground">
                  {t.adminWorkspaces.colMember}
                </span>
                <span className="t-caption text-muted-foreground">
                  {t.adminWorkspaces.colRole}
                </span>
                <span aria-hidden="true" />
              </div>

              <div className="min-h-0">
                {members.length === 0 ? (
                  <p className="px-4 py-6 text-center t-meta text-muted-foreground">
                    {t.adminWorkspaces.emptyMembers}
                  </p>
                ) : (
                  <ul className="grid gap-0.5 px-1 py-1">
                    {members.map((member) => {
                      // The sole-owner lock mirrors the server's last-owner
                      // guard (shared rule) — its role + remove are disabled.
                      const soleOwner = wouldOrphanLastOwner(
                        members,
                        member.user_id,
                        false,
                      )
                      return (
                        <li
                          className={cn(
                            MEMBER_GRID,
                            'rounded-md px-3 py-2 transition-colors hover:bg-accent/40',
                          )}
                          key={member.user_id}
                        >
                          <div className="flex min-w-0 items-center gap-2.5">
                            <InitialsAvatar
                              displayName={member.display_name}
                              email={member.email}
                            />
                            <div className="min-w-0">
                              <span className="t-list block truncate text-foreground">
                                {member.display_name ?? member.email ?? member.user_id}
                              </span>
                              {member.email ? (
                                <span className="t-meta-sm block truncate text-muted-foreground">
                                  {member.email}
                                </span>
                              ) : null}
                            </div>
                          </div>
                          <Select
                            disabled={soleOwner}
                            onValueChange={(value) =>
                              void admin.setMemberRole(
                                selected.workspace_id,
                                member.user_id,
                                value as WorkspaceRoleValue,
                              )
                            }
                            value={member.role}
                          >
                            <SelectTrigger
                              className={TABLE_ROLE_SELECT}
                              density="table"
                              title={
                                soleOwner ? t.adminWorkspaces.lastOwner : undefined
                              }
                            >
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              {ROLE_ORDER.map((role) => (
                                <SelectItem key={role} value={role}>
                                  {roleLabel(role)}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                          <Button
                            aria-label={t.adminWorkspaces.removeMember}
                            className="size-7 justify-self-end text-muted-foreground hover:text-destructive"
                            disabled={soleOwner}
                            onClick={() =>
                              void admin.removeMember(
                                selected.workspace_id,
                                member.user_id,
                              )
                            }
                            size="icon"
                            title={
                              soleOwner
                                ? t.adminWorkspaces.lastOwner
                                : t.adminWorkspaces.removeMember
                            }
                            variant="ghost"
                          >
                            <X className="icon-md" />
                          </Button>
                        </li>
                      )
                    })}
                  </ul>
                )}
              </div>

              {/* Add-member typeahead: search the tenant's users (the loaded
                  admin list — deliberately NOT the workspace-scoped
                  /v1/users/search), pick a role, click a result to assign. */}
              <div className="border-t border-border/70 bg-surface/30 px-4 py-2">
                <div className={MEMBER_GRID}>
                  <div className="relative min-w-0">
                    <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
                    <input
                      aria-label={t.adminWorkspaces.searchMember}
                      className={ADMIN_SEARCH_INPUT}
                      onChange={(event) => setAddQuery(event.target.value)}
                      placeholder={t.adminWorkspaces.searchMember}
                      type="text"
                      value={addQuery}
                    />
                  </div>
                  <Select
                    onValueChange={(value) =>
                      setAddRole(value as WorkspaceRoleValue)
                    }
                    value={addRole}
                  >
                    <SelectTrigger
                      className={TABLE_ROLE_SELECT}
                      density="table"
                    >
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {ROLE_ORDER.map((role) => (
                        <SelectItem key={role} value={role}>
                          {roleLabel(role)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <span aria-hidden="true" />
                </div>
                {addQuery.trim().length >= MIN_QUERY_LENGTH ? (
                  <div className="mt-2 overflow-hidden rounded-md border border-border bg-background">
                    {addResults.length === 0 ? (
                      <p className="px-3 py-2 t-meta text-muted-foreground">
                        {t.adminWorkspaces.searchEmpty}
                      </p>
                    ) : (
                      <ul className="max-h-44 overflow-y-auto">
                        {addResults.map((candidate) => (
                          <li key={candidate.id}>
                            <button
                              className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left hover:bg-accent"
                              onMouseDown={(event) => {
                                event.preventDefault()
                                addMemberFromResult(candidate)
                              }}
                              type="button"
                            >
                              <InitialsAvatar
                                displayName={candidate.display_name}
                                email={candidate.email}
                              />
                              <span className="min-w-0 flex-1">
                                <span className="t-list block truncate text-foreground">
                                  {candidate.display_name ??
                                    candidate.email ??
                                    candidate.id}
                                </span>
                                {candidate.email ? (
                                  <span className="t-meta-sm block truncate text-muted-foreground">
                                    {candidate.email}
                                  </span>
                                ) : null}
                              </span>
                              <Plus className="icon-sm shrink-0 text-muted-foreground" />
                            </button>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                ) : null}
              </div>
            </div>
          )}
        </section>
      </section>

      {deleteOpen && selected ? (
        <Dialog
          closeLabel={t.adminWorkspaces.cancel}
          description={t.adminWorkspaces.deleteHint}
          onClose={() => setDeleteOpen(false)}
          open
          title={t.adminWorkspaces.deleteConfirm(selected.name)}
        >
          <div className="flex items-center justify-end gap-2 pt-1">
            <Button onClick={() => setDeleteOpen(false)} size="sm" variant="ghost">
              {t.adminWorkspaces.cancel}
            </Button>
            <Button
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => {
                void admin.deleteWorkspace(selected.workspace_id)
                setDeleteOpen(false)
              }}
              size="sm"
            >
              {t.adminWorkspaces.delete}
            </Button>
          </div>
        </Dialog>
      ) : null}
    </div>
  )
}

import { useCallback, useEffect, useRef, useState } from 'react'

import {
  type AdminWorkspace,
  type WorkspaceMember,
  type WorkspaceRoleValue,
  addWorkspaceMember,
  createAdminWorkspace,
  deleteAdminWorkspace,
  listAdminWorkspaces,
  listWorkspaceMembers,
  removeWorkspaceMember,
  renameAdminWorkspace,
  setWorkspaceMemberRole,
} from '@/api/inqtrixClient'
import { DEMO_OWNER } from '@/features/sharing/demoShares'
import { seedAdminWorkspaces } from './demo'
import { wouldOrphanLastOwner } from './workspaceModel'

type Status = 'idle' | 'loading' | 'ready' | 'error'

export type AdminWorkspacesState = {
  /** True when workspace admin is in scope (instance admin, or demo). */
  available: boolean
  demo: boolean
  workspaces: AdminWorkspace[]
  status: Status
  error: string | null
  /** Last failed mutation; surfaced as a banner (No Silent Fallbacks). */
  mutationError: string | null
  /** Members cached per workspace id (loaded on selection / after a write). */
  members: Record<string, WorkspaceMember[]>
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

const LAST_OWNER_MESSAGE = 'last_owner'

/**
 * Instance workspace + membership administration for the Settings admin panel.
 * Mirrors the user/quota admin hooks: a generation-guarded reload of the
 * workspace list, members cached per workspace, a single mutation runner that
 * re-reads on success AND failure, and a demo branch that mutates local state
 * (including the last-owner guard) so the digital twin is interactive offline.
 */
export function useAdminWorkspaces({
  demo,
  enabled,
}: {
  demo: boolean
  enabled: boolean
}) {
  const [state, setState] = useState<AdminWorkspacesState>({
    available: false,
    demo,
    error: null,
    members: {},
    mutationError: null,
    status: 'idle',
    workspaces: [],
  })
  const generationRef = useRef(0)

  const reload = useCallback(async () => {
    const generation = ++generationRef.current
    if (!enabled) {
      setState({
        available: false,
        demo,
        error: null,
        members: {},
        mutationError: null,
        status: 'idle',
        workspaces: [],
      })
      return
    }
    if (demo) {
      const seed = seedAdminWorkspaces()
      setState({
        available: true,
        demo,
        error: null,
        members: Object.fromEntries(
          seed.map((entry) => [entry.workspace.workspace_id, entry.members]),
        ),
        mutationError: null,
        status: 'ready',
        workspaces: seed.map((entry) => entry.workspace),
      })
      return
    }
    setState((current) => ({ ...current, available: true, status: 'loading' }))
    try {
      const workspaces = await listAdminWorkspaces()
      if (generationRef.current !== generation) return
      setState((current) => ({
        ...current,
        available: true,
        error: null,
        status: 'ready',
        workspaces,
      }))
    } catch (error) {
      if (generationRef.current !== generation) return
      setState((current) => ({
        ...current,
        available: true,
        error: messageOf(error),
        status: 'error',
        workspaces: [],
      }))
    }
  }, [demo, enabled])

  useEffect(() => {
    void reload()
  }, [reload])

  /** Load (and cache) one workspace's members. Demo members are pre-seeded. */
  const loadMembers = useCallback(
    async (workspaceId: string) => {
      if (demo) return
      try {
        const members = await listWorkspaceMembers(workspaceId)
        setState((current) => ({
          ...current,
          members: { ...current.members, [workspaceId]: members },
        }))
      } catch (error) {
        setState((current) => ({ ...current, mutationError: messageOf(error) }))
      }
    },
    [demo],
  )

  // One runner: clear the prior error, run, then re-read the workspace list
  // AND the affected workspace's members on success OR failure (a partial
  // write must not leave a stale table) — then re-assert the error.
  const runMutation = useCallback(
    async (workspaceId: string | null, action: () => Promise<void>) => {
      setState((current) => ({ ...current, mutationError: null }))
      try {
        await action()
        await reload()
        if (workspaceId) await loadMembers(workspaceId)
      } catch (error) {
        await reload()
        if (workspaceId) await loadMembers(workspaceId)
        setState((current) => ({ ...current, mutationError: messageOf(error) }))
      }
    },
    [loadMembers, reload],
  )

  const createWorkspace = useCallback(
    (name: string) => {
      if (demo) {
        setState((current) => {
          const workspaceId = `ws-demo-${current.workspaces.length + 1}`
          const workspace: AdminWorkspace = {
            created_by_user_id: DEMO_OWNER.userId,
            member_count: 1,
            name,
            workspace_id: workspaceId,
          }
          return {
            ...current,
            members: {
              ...current.members,
              [workspaceId]: [
                {
                  display_name: DEMO_OWNER.displayName,
                  email: DEMO_OWNER.email,
                  role: 'owner',
                  user_id: DEMO_OWNER.userId,
                },
              ],
            },
            workspaces: [...current.workspaces, workspace].sort((a, b) =>
              a.name.localeCompare(b.name),
            ),
          }
        })
        return Promise.resolve(undefined as string | undefined)
      }
      setState((current) => ({ ...current, mutationError: null }))
      return createAdminWorkspace(name)
        .then((created) => {
          void reload()
          return created.workspace_id
        })
        .catch((error) => {
          // Unlike the runMutation writes, create returns the new id for
          // auto-selection, so it has its own surfacing of the failure banner.
          setState((current) => ({ ...current, mutationError: messageOf(error) }))
          return undefined
        })
    },
    [demo, reload],
  )

  const renameWorkspace = useCallback(
    (workspaceId: string, name: string) => {
      if (demo) {
        setState((current) => ({
          ...current,
          workspaces: current.workspaces
            .map((workspace) =>
              workspace.workspace_id === workspaceId
                ? { ...workspace, name }
                : workspace,
            )
            .sort((a, b) => a.name.localeCompare(b.name)),
        }))
        return Promise.resolve()
      }
      return runMutation(null, async () => {
        await renameAdminWorkspace(workspaceId, name)
      })
    },
    [demo, runMutation],
  )

  const deleteWorkspace = useCallback(
    (workspaceId: string) => {
      if (demo) {
        setState((current) => {
          const members = { ...current.members }
          delete members[workspaceId]
          return {
            ...current,
            members,
            workspaces: current.workspaces.filter(
              (workspace) => workspace.workspace_id !== workspaceId,
            ),
          }
        })
        return Promise.resolve()
      }
      return runMutation(null, async () => {
        await deleteAdminWorkspace(workspaceId)
      })
    },
    [demo, runMutation],
  )

  const addMember = useCallback(
    (
      workspaceId: string,
      member: { user_id: string; display_name: string | null; email: string | null },
      role: WorkspaceRoleValue,
    ) => {
      if (demo) {
        setState((current) => {
          const members = [
            ...(current.members[workspaceId] ?? []).filter(
              (existing) => existing.user_id !== member.user_id,
            ),
            { ...member, role },
          ].sort((a, b) => a.user_id.localeCompare(b.user_id))
          return {
            ...current,
            members: { ...current.members, [workspaceId]: members },
            workspaces: current.workspaces.map((workspace) =>
              workspace.workspace_id === workspaceId
                ? { ...workspace, member_count: members.length }
                : workspace,
            ),
          }
        })
        return Promise.resolve()
      }
      return runMutation(workspaceId, async () => {
        await addWorkspaceMember(workspaceId, member.user_id, role)
      })
    },
    [demo, runMutation],
  )

  const setMemberRole = useCallback(
    (workspaceId: string, userId: string, role: WorkspaceRoleValue) => {
      if (demo) {
        const members = state.members[workspaceId] ?? []
        if (wouldOrphanLastOwner(members, userId, role === 'owner')) {
          setState((current) => ({ ...current, mutationError: LAST_OWNER_MESSAGE }))
          return Promise.resolve()
        }
        setState((current) => ({
          ...current,
          members: {
            ...current.members,
            [workspaceId]: (current.members[workspaceId] ?? []).map((member) =>
              member.user_id === userId ? { ...member, role } : member,
            ),
          },
          mutationError: null,
        }))
        return Promise.resolve()
      }
      return runMutation(workspaceId, async () => {
        await setWorkspaceMemberRole(workspaceId, userId, role)
      })
    },
    [demo, runMutation, state.members],
  )

  const removeMember = useCallback(
    (workspaceId: string, userId: string) => {
      if (demo) {
        const members = state.members[workspaceId] ?? []
        if (wouldOrphanLastOwner(members, userId, false)) {
          setState((current) => ({ ...current, mutationError: LAST_OWNER_MESSAGE }))
          return Promise.resolve()
        }
        setState((current) => {
          const next = (current.members[workspaceId] ?? []).filter(
            (member) => member.user_id !== userId,
          )
          return {
            ...current,
            members: { ...current.members, [workspaceId]: next },
            mutationError: null,
            workspaces: current.workspaces.map((workspace) =>
              workspace.workspace_id === workspaceId
                ? { ...workspace, member_count: next.length }
                : workspace,
            ),
          }
        })
        return Promise.resolve()
      }
      return runMutation(workspaceId, async () => {
        await removeWorkspaceMember(workspaceId, userId)
      })
    },
    [demo, runMutation, state.members],
  )

  return {
    addMember,
    createWorkspace,
    deleteWorkspace,
    loadMembers,
    reload,
    removeMember,
    renameWorkspace,
    setMemberRole,
    state,
  }
}

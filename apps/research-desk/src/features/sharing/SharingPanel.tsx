import { useState, type ReactNode } from 'react'
import {
  BookOpen,
  Check,
  ExternalLink,
  FileText,
  Globe2,
  Inbox,
  Library,
  Trash2,
  Users,
  X,
  type LucideIcon,
} from '@/components/icons'
import { InitialsAvatar } from '@/components/ui/avatar'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { SettingsSection, StatusBadge } from '@/features/settings/parts'
import { useLocale } from '@/i18n/LocaleProvider'
import { countLabel } from '@/i18n/plural'
import type { TranslationDictionary } from '@/i18n/translations'
import { ShareDialog } from './ShareDialog'
import { sharePermissionLabel } from './shareModel'
import type { InboxShare, OutgoingShare, SharePermissionValue } from './types'
import type { SharingInboxHandle } from './useSharingInbox'

type SharingPanelProps = {
  /** Demo resolves the inbox + dialog from seeded data (no backend). */
  demo: boolean
  /** Navigate to and focus the exact accepted resource. */
  onOpen: (share: InboxShare) => void
  /** The current user, shown as owner in the reused share dialog. */
  ownerEmail: string | null
  ownerName: string | null
  refreshToken: number
  sharing: SharingInboxHandle
}

const KIND_ICON: Record<string, LucideIcon> = {
  knowledge_collection: BookOpen,
  editor_document: FileText,
  prompt_template: Library,
  skill_template: Library,
  run: Globe2,
}

function kindIcon(resourceType: string): LucideIcon {
  return KIND_ICON[resourceType] ?? FileText
}

function kindLabel(
  t: TranslationDictionary,
  locale: 'de' | 'en',
  resourceType: string,
): string {
  switch (resourceType) {
    case 'run':
      return t.sharingManagement.kindRun
    case 'knowledge_collection':
      return t.sharingManagement.kindCollection
    case 'prompt_template':
      return t.sharingManagement.kindTemplate
    case 'skill_template':
      return t.skills.title
    case 'editor_document':
      return locale === 'de' ? 'Editor-Dokument' : 'Editor document'
    default:
      return t.sharingManagement.kindOther
  }
}

function permissionLabel(
  t: TranslationDictionary,
  locale: 'de' | 'en',
  permission: SharePermissionValue,
): string {
  return sharePermissionLabel(permission, locale, {
    edit: t.sharingManagement.permissionEdit,
    view: t.sharingManagement.permissionView,
  })
}

function grantorName(share: InboxShare): string {
  return share.granted_by_display_name ?? share.granted_by_user_id
}

function peopleLabel(t: TranslationDictionary, count: number): string {
  return countLabel(count, t.sharingManagement.onePerson, t.sharingManagement.peopleCount)
}

function Row({
  leading,
  subtitle,
  title,
  trailing,
}: {
  leading: ReactNode
  subtitle: string
  title: string
  trailing: ReactNode
}) {
  return (
    <div className="flex items-center gap-3 rounded-md px-3 py-2.5 transition-colors hover:bg-surface/45">
      <div className="shrink-0">{leading}</div>
      <div className="min-w-0 flex-1">
        <p className="truncate t-list text-foreground">{title}</p>
        <p className="truncate t-meta-sm text-muted-foreground">{subtitle}</p>
      </div>
      <div className="flex shrink-0 items-center gap-1.5">{trailing}</div>
    </div>
  )
}

function SectionEmpty({ icon: Icon, label }: { icon: LucideIcon; label: string }) {
  return (
    <div className="flex flex-col items-center gap-2 px-3 py-7 text-center">
      <Icon className="icon-md text-muted-foreground/55" />
      <p className="t-meta text-muted-foreground">{label}</p>
    </div>
  )
}

/**
 * The sharing-management settings sub-panel: three sections built on the
 * recipient inbox + outgoing listing. "Eingegangen" is the consent queue
 * (accept / decline), "Mit mir geteilt" the accepted shares (leave), and "Von
 * mir geteilt" the caller's outgoing shares (manage via the existing share
 * dialog — one sharing surface, no second UI). Container-mode only; the gate
 * lives in the desk, which only mounts this when sharing is enabled.
 */
export function SharingPanel({
  demo,
  onOpen,
  ownerEmail,
  ownerName,
  refreshToken,
  sharing,
}: SharingPanelProps) {
  const { locale, t } = useLocale()
  const { accepted, mutationError, outgoing, pending, status } = sharing.state
  const [confirm, setConfirm] = useState<
    { mode: 'decline' | 'leave'; share: InboxShare } | null
  >(null)
  const [manage, setManage] = useState<OutgoingShare | null>(null)

  const confirmDrop = () => {
    if (confirm) void sharing.drop(confirm.share.id).catch(() => undefined)
    setConfirm(null)
  }

  return (
    <div className="flex flex-col gap-4">
      {status === 'error' ? (
        <p className="rounded-md border border-destructive/25 bg-destructive/10 px-3 py-2 t-meta text-destructive">
          {t.sharingManagement.loadError}
        </p>
      ) : null}
      {mutationError ? (
        <p className="rounded-md border border-destructive/25 bg-destructive/10 px-3 py-2 t-meta text-destructive">
          {t.sharingManagement.actionError}
        </p>
      ) : null}

      <SettingsSection
        description={t.sharingManagement.incomingDescription}
        title={t.sharingManagement.incomingTitle}
      >
        {pending.length === 0 ? (
          <SectionEmpty icon={Inbox} label={t.sharingManagement.incomingEmpty} />
        ) : (
          pending.map((share) => (
            <Row
              key={share.id}
              leading={
                <InitialsAvatar
                  displayName={share.granted_by_display_name}
                  email={null}
                />
              }
              title={share.resource_title}
              subtitle={`${kindLabel(t, locale, share.resource_type)} · ${t.sharingManagement.sharedBy.replace('{name}', grantorName(share))} · ${permissionLabel(t, locale, share.permission)}`}
              trailing={
                <>
                  <StatusBadge
                    density="table"
                    label={t.sharingManagement.pendingBadge}
                    tone="warning"
                  />
                  <Button onClick={() => void sharing.accept(share.id)} size="sm">
                    <Check />
                    {t.sharingManagement.accept}
                  </Button>
                  <Button
                    aria-label={t.sharingManagement.decline}
                    onClick={() => setConfirm({ mode: 'decline', share })}
                    size="sm"
                    variant="ghost"
                  >
                    <X />
                  </Button>
                </>
              }
            />
          ))
        )}
      </SettingsSection>

      <SettingsSection
        description={t.sharingManagement.sharedWithMeDescription}
        title={t.sharingManagement.sharedWithMeTitle}
      >
        {accepted.length === 0 ? (
          <SectionEmpty
            icon={Inbox}
            label={t.sharingManagement.sharedWithMeEmpty}
          />
        ) : (
          accepted.map((share) => (
            <Row
              key={share.id}
              leading={
                <InitialsAvatar
                  displayName={share.granted_by_display_name}
                  email={null}
                />
              }
              title={share.resource_title}
              subtitle={`${kindLabel(t, locale, share.resource_type)} · ${t.sharingManagement.sharedBy.replace('{name}', grantorName(share))} · ${permissionLabel(t, locale, share.permission)}`}
              trailing={
                <>
                  <StatusBadge
                    density="table"
                    label={t.sharingManagement.activeBadge}
                    tone="success"
                  />
                  <Button
                    onClick={() => onOpen(share)}
                    size="sm"
                    variant="ghost"
                  >
                    <ExternalLink />
                    {t.sharingManagement.open}
                  </Button>
                  <Button
                    aria-label={t.sharingManagement.leave}
                    className="text-muted-foreground hover:text-destructive"
                    onClick={() => setConfirm({ mode: 'leave', share })}
                    size="sm"
                    variant="ghost"
                  >
                    <Trash2 />
                  </Button>
                </>
              }
            />
          ))
        )}
      </SettingsSection>

      <SettingsSection
        description={t.sharingManagement.sharedByMeDescription}
        title={t.sharingManagement.sharedByMeTitle}
      >
        {outgoing.length === 0 ? (
          <SectionEmpty icon={Users} label={t.sharingManagement.sharedByMeEmpty} />
        ) : (
          outgoing.map((item) => {
            const Icon = kindIcon(item.resource_type)
            return (
              <Row
                key={`${item.resource_type}:${item.resource_id}`}
                leading={
                  <span className="grid size-7 place-items-center rounded-md bg-surface text-muted-foreground">
                    <Icon className="icon-sm" />
                  </span>
                }
                title={item.resource_title}
                subtitle={`${kindLabel(t, locale, item.resource_type)} · ${peopleLabel(t, item.share_count)}`}
                trailing={
                  <>
                    {item.pending_count > 0 ? (
                      <StatusBadge
                        density="table"
                        label={t.sharingManagement.pendingCount.replace(
                          '{count}',
                          String(item.pending_count),
                        )}
                        tone="neutral"
                      />
                    ) : null}
                    <Button
                      onClick={() => setManage(item)}
                      size="sm"
                      variant="outline"
                    >
                      {t.sharingManagement.manage}
                    </Button>
                  </>
                }
              />
            )
          })
        )}
      </SettingsSection>

      <Dialog
        description={
          confirm?.mode === 'leave'
            ? t.sharingManagement.leaveBody
            : t.sharingManagement.declineBody
        }
        footer={
          <>
            <Button onClick={() => setConfirm(null)} variant="ghost">
              {t.sharingManagement.cancel}
            </Button>
            <Button onClick={confirmDrop} variant="destructive">
              {t.sharingManagement.confirm}
            </Button>
          </>
        }
        onClose={() => setConfirm(null)}
        open={confirm !== null}
        title={
          confirm?.mode === 'leave'
            ? t.sharingManagement.leaveTitle
            : t.sharingManagement.declineTitle
        }
      >
        <p className="t-body text-muted-foreground">{confirm?.share.resource_title}</p>
      </Dialog>

      {manage ? (
        <ShareDialog
          demo={demo}
          onChanged={() => void sharing.reload()}
          onClose={() => setManage(null)}
          ownerEmail={ownerEmail}
          ownerName={ownerName}
          refreshToken={refreshToken}
          resourceId={manage.resource_id}
          resourceTitle={manage.resource_title}
          resourceType={manage.resource_type}
        />
      ) : null}
    </div>
  )
}

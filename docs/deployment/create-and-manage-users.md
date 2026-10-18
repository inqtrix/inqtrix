# Create and manage users

## Scope

How accounts come into being and how an admin manages them — across every auth
mode, and through the web app's Admin section. Covers first-owner
setup (`local`), adding accounts, instance roles, disabling, and personal
access tokens. It does **not** cover workspace-level sharing roles (a separate
concept — see below) or the OIDC wiring itself (see
[Auth modes](auth-modes.md)).

## How a user arrives, per mode

There is no single "create user" button that fits all modes — where identity
lives depends on the auth mode.

| Mode | User store | First user | More users | Sharing |
|---|---|---|---|---|
| `none` | none (one anonymous principal) | – | – | no (no scoped identity) |
| `apikey` | none (one static principal) | – | – | no |
| `local` | Postgres + argon2id | **first-run owner setup** | admin creates them (initial password) | yes |
| `oidc` | your IdP + a JIT mirror | first login (or allowed group) | IdP + invitations | yes |

`local` is the only mode with an in-app "create account" action, and it is the
default of the Stack Compose recipe rather than of the server itself (the server
default is `infer`, which resolves to `apikey` when `INQTRIX_SERVER_API_KEY` is
set and to `none` otherwise); the single-operator `none`/`apikey` modes have no
scoped identity and therefore no user management or sharing.

## Two role concepts — do not conflate them

- **Instance role** (`admin` / `user`) — instance-wide. `admin` unlocks the
  Admin section. The `/api/auth/session` payload carries the instance role as
  `user.role` (nested under `user`, not at the top level); the UI gate is
  default-closed (anything that is not exactly `admin` yields no admin surface).
- **Workspace role** (viewer / commenter / editor / owner) — membership and
  administration inside one workspace. It never implies access to a run,
  collection, prompt, or skill; those resources use explicit direct
  `view`/`edit` shares. Workspace membership only limits who may share when
  `INQTRIX_SHARING_RESTRICT_TO_WORKSPACE_MEMBERS=true`.

Promoting someone to instance `admin` does not change their workspace roles, and
vice versa.

### Multiple admins do not require workspaces

An instance may have two, three, or more active admins. Creating an account as
an instance admin does **not** create a workspace and does not require a
workspace membership. An instance admin without any workspace membership can
administer users, quotas, and workspaces. Quotas are independent as well. With
`INQTRIX_QUOTA_ENABLED=false`, adding users or admins does not activate
metering or impose an implicit limit. Details:
[Auth modes](auth-modes.md#instance-admin-workspace-management) and
[quota administration](auth-modes.md#instance-admin-quota-administration).

## `local`: first-run owner setup

On the first start of a `local` deployment the web app shows an **owner setup**
screen instead of the lock screen. The account you create there:

- becomes the **instance admin**;
- is logged in immediately (the server sets the session cookie);
- is the only owner — the setup screen never appears again (a second attempt is
  refused, race-safe).

The gate is driven by `GET /api/auth/config`, whose `registration.needs_owner`
flag is `true` only in `local` mode with no owner yet; `GET /api/setup/status`
answers from the same credential store, but it is registered in `local` mode
alone — every other mode returns `404` rather than `false`.

```bash
# Probe the gate (local mode, fresh DB)
curl -s http://localhost:8080/api/setup/status        # -> {"needs_owner":true}
```

## Adding more accounts (`local`)

An admin opens **Settings → Admin → Users → Create user** and enters an
email, optional display name, an initial password (the dialog can generate a
strong one), and a role. The initial password is shown **once** — copy it and
share it securely with the person; it is never retrievable again. There is no
email delivery; you hand the credentials over out-of-band.

Behind the UI this is `POST /v1/admin/users` (admin-session-only; a personal
access token can never administer users):

```bash
curl -s -X POST http://localhost:8080/v1/admin/users \
  -H 'Content-Type: application/json' -H "X-CSRF-Token: $CSRF" -b cookies.txt \
  -d '{"email":"mara@example.com","password":"correct-horse-battery-staple","instance_role":"user"}'
```

Account-creation policy is `INQTRIX_LOCAL_REGISTRATION` — `closed` (the
default) or `open`. Neither setting mounts a public signup route: `open` only
makes `GET /api/auth/config` report `registration.self_service: true`, an
advisory hint for a frontend. Accounts are always created through the
first-run owner setup or by an admin.

For `oidc`, users already live in the directory/IdP — there is no
local "create"; they appear in the mirror on first login.

## Managing users (all multi-user modes)

In **Settings → Admin → Users** an admin can, per row:

- **Change the instance role** (admin ↔ user) via the role select;
- **Disable / enable** via the status switch — disabling is a complete cut-off:
  the mirror flag is set, live sessions are purged, personal access tokens are
  revoked, and (for `local`) the password credential is disabled so login is
  refused. Re-enabling permits a fresh login but restores no old session or
  token; the user must sign in again and create new tokens when needed.

Two invariants protect the deployment from locking itself out, enforced
server-side (atomic) and reflected in the UI (the control is disabled with a
tooltip):

- you **cannot disable or demote yourself** (the "Sie"/"You" row);
- the **last active admin** can be neither demoted nor disabled.

### Erasing an account

Erasing removes an account together with everything it owns. It needs the
PostgreSQL backend and the deletion worker; the memory backend answers `503`.

**Preview first.** `GET /v1/admin/users/{id}/erasure-preview` counts what the
erasure reaches, rule by rule: the rows that are deleted (`deleted_rows`),
the aggregates that go through their own deletion (`delegated_rows`: library
sections with their files and blobs, knowledge collections and documents with
their vectors,
vector indexes, Agent and Knowledge Desk sessions, runs with their
checkpoints), the rows in other people's work that
move to the "Deleted account" placeholder (`reassigned_rows`) or lose their
reference to the account (`cleared_rows`), and anything that blocks the
erasure (`blockers`). `foreign_rows` counts the comments colleagues wrote
inside the account's own documents: those documents are deleted, and the
comments in them go with them.

**Start.** `DELETE /v1/admin/users/{id}` answers `202` with a deletion
operation. Follow it with `GET /v1/deletion-operations/{operation_id}`, sent
without a workspace, until its status is `deleted`. The deletion worker runs
it in steps:

1. **Cut off.** In the same transaction that starts the erasure, the
   account's sessions end, its personal access tokens are revoked and its
   password is refused. From then on every sign-in, enable or role change
   of the account is refused (`409 erasure_in_progress`), and the user list
   carries the operation in `erasure_operation_id`.
2. **Aggregates.** Data outside PostgreSQL goes first, through the same
   deletion operations the owner's own delete would start: the library
   section by section, each with its files and their blobs, knowledge
   documents the account added to other people's collections, its own
   collections with their vectors, vector indexes, sessions, and runs with
   their checkpoints. Last come sources that outlived their file, section or
   document: a source still marked live although nothing it belonged to
   exists any more. Each goes as a deletion of its own (target kind
   `source`): the Knowledge store removes whatever it still holds for that
   source, the erasure verifies that nothing remains, and the source keeps
   its tombstone. Without the Knowledge service such a deletion fails rather
   than assume the source is empty.
3. **Rows.** The account's own rows are deleted in short batches, its
   comments in other people's documents included. Rows that belong to other
   people keep their content: a mention of the account is removed from a
   colleague's comment; an edit or a resolved comment thread in somebody
   else's document is attributed to the tenant's "Deleted account"
   placeholder, which is disabled, has no credential and is not listed; any
   other reference is cleared.
4. **End.** When nothing but the retained records names the account, the
   account row is removed.

Only the first and the last step take the tenant's security lock, each for
one short transaction like any other account change. The steps in between
hold no lock that sign-ins or other people's work wait for, so a large
account can be erased while everyone keeps working.

**What stays.** The audit log keeps every entry with its pseudonymous actor;
the account id it named becomes unresolvable. The usage ledger keeps its
rows until its own retention (`INQTRIX_USAGE_RETENTION_DAYS`) removes them.
Every source the erasure deleted (a library section, a file, a knowledge
document) keeps the tombstone its deletion leaves: a row in
`source_lifecycles` with the source id, the account id and the state
`deleted`, and no content. It makes sure a late request cannot bring the
source back; the account id in it becomes unresolvable as well.

**Refusals** (`409`):

- `self_erase`: an admin cannot erase their own account;
- `last_admin`: the last active admin cannot be erased;
- `erasure_refused`: the account created a workspace that other people work
  in. `reasons` lists these workspaces with the same lines the preview shows
  as `blockers`; reassign or delete them first;
- `deletion_in_progress`: another administrator started erasing this account,
  and only that administrator can follow or retry the operation.

**Failure and retry.** If a step cannot finish, for example because a store
is unreachable or a run of the account is still stopping, the operation ends
as `delete_failed` with the reason, and the account stays cut off. Deleting
the account again resumes the same operation where it stopped; nothing that
is already gone is deleted twice.

**In the UI.** **Settings → Admin → Users → (row) → Delete account** shows
the preview and asks for the account's address before it starts. The row
then reads "Deleting" with its step and count (for example "Content
removed (2/5)") and leaves the list when the account is gone; its role,
status, password and delete controls are off meanwhile. A failed erasure
shows the reason and **Try again**. The list follows up to ten erasures at a
time; further accounts read "Deleting" until one of them is done. Where
an erased account's edits stay in somebody else's document, the document's
activity names them "Deleted account" in the reader's language.

## Passwords (local mode)

Passwords must be at least 12 characters: owner setup, admin-created accounts,
admin password resets, and a user's own password change all reject a shorter one
with `400`, and the dialog shows the same "min. 12" hint. Length is the only
hard rule — the character-class hints beside the field feed the strength meter
and never block submission.

- **Change your own password** — a signed-in local user opens **Settings →
  Account → Security → Change password**, enters the current password and a new
  one. The session stays valid. (`oidc` passwords live upstream, so this
  only appears for `local`.)
- **Forgot a password** — there is no email reset; an admin sets a new one in
  **Settings → Admin → Users → (row) → Reset password**. The new
  password is shown **once** (copy + hand over securely); the user's live
  sessions are ended so the old password stops working immediately. PATs are
  left intact (a reset is not a full disable). To cut off all access instead,
  disable the account.
- **Brute-force protection** — repeated failed logins for the same
  `(email, IP)` are throttled (10 attempts / 5 min → 60 s lockout by default,
  `INQTRIX_LOGIN_RATE_LIMIT_*`); behind a proxy, also rate-limit at the edge
  ([Deploy to production](production-checklist.md#login-rate-limiting)).

## Personal access tokens

Any signed-in user (not just admins) manages **their own** API tokens under
**Settings → Account → Access tokens** — list, create, revoke. A created
token's plaintext is shown **once**; copy it immediately. Use it as a Bearer
token for the native API:

```bash
curl -s http://localhost:8080/v1/runs -H "Authorization: Bearer ipat_..."
```

Tokens are session-scoped to manage (a token can never mint or revoke tokens),
and a disabled user's tokens are revoked by the disable cascade.

## Related docs

- [Auth modes](auth-modes.md) — choosing a mode; how each authenticates.
- [Build a UI on Inqtrix](../extending/ui.md) — the native `/v1/runs` API and PATs.
- [Stack quickstart](../getting-started/stack-quickstart.md) — one-command start.

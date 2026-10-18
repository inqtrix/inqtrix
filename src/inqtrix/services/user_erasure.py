"""What deleting a user account touches, and what happens to each row.

Deleting a person is the one destructive action in this product that
reaches every subsystem at once.  Forty-nine foreign keys point at
``users(id)`` and every one of them is ``ON DELETE RESTRICT`` on purpose:
migration 0045 refuses to install a users foreign key that is anything
else, because a silent cascade through shared documents is worse than an
account that cannot be removed.  That deliberate wall is why an erasure
needs a written plan instead of a ``DELETE`` statement.

Three facts shape the plan, each measured rather than assumed:

* **Not every row that names a user is theirs.**  A comment they wrote
  lives inside somebody else's document; a quota limit they set belongs
  to the person it limits; an approval they decided belongs to another
  person's run.  Those rows get the reference cleared, not deleted --
  otherwise erasing one account quietly deletes the work of colleagues
  who were never asked.
* **Two tables are append-only by grant.**  The runtime role holds only
  ``INSERT``/``SELECT`` on ``audit_log`` and ``llm_usage``, so the
  application cannot remove those rows even if it wanted to.  The audit
  trail is designed for exactly this: its read model shows the actor
  only as ``actor_pseudonym``, and ``actor_user_id`` is nullable, so
  releasing the foreign key de-identifies history without losing it.
* **Blobs and vectors live outside Postgres and are only reachable
  through the rows being deleted.**  Object keys and Qdrant point ids are
  derived from the canonical rows, so the rows are the map: delete them
  first and the external data becomes permanently unreachable.  Those
  aggregates are therefore delegated to the durable deletion machine
  that already owns that ordering, never re-implemented here: the
  account's deletion operation runs one ordinary aggregate deletion per
  session, library section, collection and index before any row goes.
  The machine keeps one record on purpose: a source it deleted stays as
  a content-free tombstone, so a late request can never resurrect it.

Two columns cannot simply lose their pointer: a human collaboration
update and a resolved comment thread carry a CHECK that names a human.
Inside somebody else's document they move to the tenant's
deleted-account placeholder instead -- the colleague's document keeps
its contiguous change chain, and nothing is attributed to a living
person.

The plan below is the single definition of all of it.  It is checked
against the schema by a gate test, so a table added later with a user
column fails loudly here instead of being silently missed by an erasure
that still reports success.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Disposition(StrEnum):
    """What an erasure does with one user-referencing column."""

    DELEGATE = "delegate"
    """Owned aggregate with data outside Postgres; the durable deletion
    machine removes the blobs/vectors AND the rows, in its own order."""

    DELETE_ROW = "delete_row"
    """The row IS the user's: it goes."""

    CLEAR_REFERENCE = "clear_reference"
    """The row belongs to somebody else or to the instance; only the
    pointer at the departing user is removed."""

    REASSIGN = "reassign"
    """The row belongs to somebody else and a CHECK requires it to name a
    human; the pointer moves to the tenant's deleted-account placeholder."""

    RETAIN = "retain"
    """Append-only by grant. The row stays and the reference is released
    by the database itself (``ON DELETE SET NULL``) or outlives the
    account under its own documented retention."""

    REFUSE = "refuse"
    """No correct answer exists without a human decision; the erasure
    stops and names the blocking rows instead of guessing."""


@dataclass(frozen=True)
class ErasureRule:
    """One column, one disposition, and the reason a reader can check."""

    table: str
    column: str
    disposition: Disposition
    reason: str
    #: Aggregate the durable deletion machine owns (DELEGATE only).
    aggregate: str = ""
    #: ``(column, value)`` of the rows the deletion machine keeps on
    #: purpose once it finished: its tombstones, ids without content. The
    #: rule neither counts them nor names them as residuals.
    tombstone: tuple[str, str] | None = None


# Order matters and is not alphabetical: delegated aggregates run first,
# because their blob and vector cleanup needs the canonical rows that
# later steps delete.  Within Postgres, children precede parents.
ERASURE_PLAN: tuple[ErasureRule, ...] = (
    # -- delegated aggregates (blobs, vectors, and their rows) ---------- #
    ErasureRule(
        "files", "owner_user_id", Disposition.DELEGATE,
        "Uploaded files carry object-store blobs whose keys exist only on "
        "these rows; the section holding their asset deletes them with it.",
        aggregate="section",
    ),
    ErasureRule(
        "asset_records", "created_by_user_id", Disposition.DELEGATE,
        "An asset always lives in a section of its own account and "
        "workspace, so deleting that section deletes the asset, its file "
        "and its blob.",
        aggregate="section",
    ),
    ErasureRule(
        "asset_sections", "created_by_user_id", Disposition.DELEGATE,
        "A section is the library's root: its deletion removes its groups "
        "and assets with their blobs and vectors before the section row.",
        aggregate="section",
    ),
    ErasureRule(
        "asset_groups", "created_by_user_id", Disposition.DELEGATE,
        "A group lives inside a section and goes with it.",
        aggregate="section",
    ),
    ErasureRule(
        "source_lifecycles", "owner_user_id", Disposition.DELEGATE,
        "Tracks the object-store lifecycle of one source. Its deletion "
        "ends in a tombstone that stays, so a late request can never "
        "resurrect the source; a source still live is a residual. A live "
        "source whose asset, section or document is gone is settled as a "
        "deletion of its own.",
        aggregate="source",
        tombstone=("state", "deleted"),
    ),
    ErasureRule(
        "source_lifecycles", "owner_key", Disposition.DELEGATE,
        "The owner key IS the object-store prefix, so the blobs are only "
        "reachable while this row exists; a finished deletion's tombstone "
        "stays like the one above.",
        aggregate="source",
        tombstone=("state", "deleted"),
    ),
    ErasureRule(
        "upload_operations", "created_by_user_id", Disposition.DELEGATE,
        "An interrupted upload can still hold blobs; the section's "
        "deletion settles it together with the asset it fills.",
        aggregate="section",
    ),
    ErasureRule(
        "knowledge_collections", "created_by_user_id", Disposition.DELEGATE,
        "A collection owns Qdrant points that only its rows can name.",
        aggregate="knowledge_collection",
    ),
    ErasureRule(
        "knowledge_documents", "source_owner_user_id", Disposition.DELEGATE,
        "Documents and their chunks are removed with their collection.",
        aggregate="knowledge_collection",
    ),
    ErasureRule(
        "vector_index_records", "created_by_user_id", Disposition.DELEGATE,
        "An index owns vectors outside Postgres and detaches before its "
        "rows go.",
        aggregate="vector_index",
    ),
    ErasureRule(
        "agent_sessions", "created_by_user_id", Disposition.DELEGATE,
        "A session owns runs, artifacts and plan rows the aggregate "
        "settles in order.",
        aggregate="agent_session",
    ),
    ErasureRule(
        "knowledge_sessions", "created_by_user_id", Disposition.DELEGATE,
        "Same shape as an agent session, with its own aggregate.",
        aggregate="knowledge_session",
    ),
    ErasureRule(
        "runs", "created_by_user_id", Disposition.DELEGATE,
        "Runs the user started that no session aggregate already claimed. "
        "A run's checkpoints sit outside the foreign-key graph, so the run "
        "store deletes each run together with them.",
        aggregate="run",
    ),
    ErasureRule(
        "indexing_jobs", "created_by_user_id", Disposition.DELETE_ROW,
        "A job is a record of work on data that is going anyway; a running "
        "one is refused by the precondition gate, not cancelled here.",
    ),
    ErasureRule(
        "deletion_operations", "created_by_user_id", Disposition.DELETE_ROW,
        "Receipts of the user's OWN past deletions; the receipt of this "
        "erasure belongs to the administrator and is untouched.",
    ),
    ErasureRule(
        "deletion_operation_assets", "created_by_user_id",
        Disposition.DELETE_ROW,
        "Manifest rows of those receipts.",
    ),

    # -- editor and collaboration --------------------------------------- #
    ErasureRule(
        "editor_collaboration_comment_reads", "user_id",
        Disposition.DELETE_ROW,
        "Read markers are personal state and say when this person read "
        "what.",
    ),
    ErasureRule(
        "editor_collaboration_leases", "user_id", Disposition.DELETE_ROW,
        "An editing lease is a live claim; releasing it frees the "
        "document for everyone else.",
    ),
    ErasureRule(
        "editor_collaboration_comment_messages", "author_user_id",
        Disposition.DELETE_ROW,
        "Messages the user wrote, including those inside other people's "
        "documents.",
    ),
    ErasureRule(
        "editor_collaboration_comment_messages", "mention_user_ids",
        Disposition.CLEAR_REFERENCE,
        "Mentions of the departing user sit inside messages OTHER people "
        "wrote; only the mention is stripped.",
    ),
    ErasureRule(
        "editor_collaboration_comment_threads", "created_by_user_id",
        Disposition.DELETE_ROW,
        "Threads the user opened, with their replies.",
    ),
    ErasureRule(
        "editor_collaboration_comment_threads", "resolved_by_user_id",
        Disposition.REASSIGN,
        "Resolving somebody else's thread does not make the thread "
        "theirs, and a resolved thread must name its resolver: the "
        "deleted-account placeholder takes that place.",
    ),
    ErasureRule(
        "editor_collaboration_updates", "actor_user_id",
        Disposition.REASSIGN,
        "A collaboration update is one link in a document's change chain "
        "and the loader verifies that chain is unbroken; a human edit must "
        "name a human, so in a colleague's document it moves to the "
        "deleted-account placeholder. Updates inside the user's OWN "
        "documents go with the document.",
    ),
    ErasureRule(
        "editor_documents", "created_by_user_id", Disposition.DELETE_ROW,
        "Documents the user owns, with everything anchored to them.",
    ),
    ErasureRule(
        "editor_folders", "created_by_user_id", Disposition.DELETE_ROW,
        "Folders the user owns; their documents are removed by the rule "
        "above, so the folder is left empty and goes with them.",
    ),
    ErasureRule(
        "editor_comments", "created_by_user_id", Disposition.DELETE_ROW,
        "Comments only the user could see, on documents that may belong to "
        "anybody.",
    ),
    ErasureRule(
        "editor_document_share_links", "created_by_user_id",
        Disposition.DELETE_ROW,
        "Guest links the user issued; each is revoked before it is "
        "removed so open guest sessions close rather than dangle.",
    ),
    ErasureRule(
        "editor_patches", "created_by_user_id", Disposition.DELETE_ROW,
        "Patches the user proposed.",
    ),
    ErasureRule(
        "editor_patches", "decided_by_user_id", Disposition.CLEAR_REFERENCE,
        "Deciding somebody else's patch does not make the patch theirs.",
    ),

    # -- chat, runs, agent ---------------------------------------------- #
    ErasureRule(
        "chat_threads", "created_by_user_id", Disposition.DELETE_ROW,
        "Chat threads and their messages.",
    ),
    ErasureRule(
        "chat_thread_groups", "created_by_user_id", Disposition.DELETE_ROW,
        "Chat folders the user made; the threads inside them are removed by "
        "the rule above.",
    ),
    ErasureRule(
        "agent_session_groups", "created_by_user_id", Disposition.DELETE_ROW,
        "Agent-session folders the user made; the sessions inside them are "
        "removed by their own aggregate.",
    ),
    ErasureRule(
        "knowledge_session_groups", "created_by_user_id",
        Disposition.DELETE_ROW,
        "Knowledge session folders.",
    ),
    ErasureRule(
        "runs", "execution_actor_user_id", Disposition.CLEAR_REFERENCE,
        "Executing a run somebody else owns does not make the run theirs.",
    ),
    ErasureRule(
        "run_approvals", "decided_by_user_id", Disposition.CLEAR_REFERENCE,
        "An approval on another person's run stays; who decided it moves "
        "to the audit trail alone.",
    ),
    ErasureRule(
        "run_clarifications", "answered_by_user_id",
        Disposition.CLEAR_REFERENCE,
        "Same as an approval: the answer belongs to the run, not the "
        "answerer.",
    ),
    ErasureRule(
        "agent_feedback", "user_id", Disposition.DELETE_ROW,
        "Personal feedback on agent answers.",
    ),
    ErasureRule(
        "agent_memory_candidates", "user_id", Disposition.DELETE_ROW,
        "Remembered facts about this person.",
    ),

    # -- library and settings ------------------------------------------- #
    ErasureRule(
        "prompt_templates", "owner_user_id", Disposition.DELETE_ROW,
        "Prompt templates the user owns.",
    ),
    ErasureRule(
        "prompt_template_seed_markers", "user_id", Disposition.DELETE_ROW,
        "Marker that the stock templates were seeded for this user.",
    ),
    ErasureRule(
        "skill_templates", "owner_user_id", Disposition.DELETE_ROW,
        "Skills the user wrote. A skill shared with others is a copy on their "
        "side, so removing the original takes nothing from them.",
    ),
    ErasureRule(
        "account_preferences", "user_id", Disposition.DELETE_ROW,
        "Personal interface settings.",
    ),

    # -- access, identity, quota ---------------------------------------- #
    ErasureRule(
        "resource_shares", "recipient_user_id", Disposition.DELETE_ROW,
        "Access other people granted TO this user ends with the account.",
    ),
    ErasureRule(
        "resource_shares", "granted_by_user_id", Disposition.DELETE_ROW,
        "A grant the user made cannot survive without its grantor: the "
        "column is NOT NULL. The complete grant history stays in the "
        "audit trail, which records every grant, accept and revoke "
        "independently.",
    ),
    ErasureRule(
        "resource_shares", "revoked_by_user_id", Disposition.CLEAR_REFERENCE,
        "Revoking somebody else's share leaves only an actor reference.",
    ),
    ErasureRule(
        "workspace_members", "user_id", Disposition.DELETE_ROW,
        "Membership ends with the account.",
    ),
    ErasureRule(
        "workspaces", "created_by_user_id", Disposition.REFUSE,
        "A workspace other people still work in cannot lose its creator: "
        "the column is NOT NULL and the workspace's content is not the "
        "departing user's to delete. The erasure stops and names the "
        "workspaces so a human can reassign or remove them first.",
    ),
    ErasureRule(
        "invitations", "invited_by_user_id", Disposition.DELETE_ROW,
        "An invitation the user sent; the column is NOT NULL.",
    ),
    ErasureRule(
        "invitations", "accepted_by_user_id", Disposition.CLEAR_REFERENCE,
        "Accepting somebody else's invitation leaves only an actor "
        "reference.",
    ),
    ErasureRule(
        "auth_sessions", "user_id", Disposition.DELETE_ROW,
        "Browser sessions; removing them signs the account out everywhere.",
    ),
    ErasureRule(
        "local_credentials", "user_id", Disposition.DELETE_ROW,
        "The password record; without it the account cannot authenticate even "
        "if a row somewhere were missed.",
    ),
    ErasureRule(
        "personal_access_tokens", "owner_user_id", Disposition.DELETE_ROW,
        "API tokens the user issued.",
    ),
    ErasureRule(
        "user_authorization_generations", "user_id", Disposition.DELETE_ROW,
        "The counter that invalidates this user's cached authorization.",
    ),
    ErasureRule(
        "user_events", "target_user_id", Disposition.DELETE_ROW,
        "The per-user notification and invalidation ledger. It is ABOUT "
        "the user and has no pseudonym column to fall back on, so it "
        "cannot be de-identified the way the audit trail can.",
    ),
    ErasureRule(
        "quota_usage_counters", "subject_user_id", Disposition.DELETE_ROW,
        "This user's consumption counters.",
    ),
    ErasureRule(
        "quota_usage_adjustments", "subject_user_id", Disposition.DELETE_ROW,
        "Manual corrections to those counters.",
    ),
    ErasureRule(
        "quota_stock_lifecycles", "subject_user_id", Disposition.DELETE_ROW,
        "Stock allocations held for this user.",
    ),
    ErasureRule(
        "quota_limits", "subject_user_id", Disposition.DELETE_ROW,
        "Quota limits set ON this user; with the account gone there is nobody "
        "left to limit.",
    ),
    ErasureRule(
        "quota_limits", "set_by_user_id", Disposition.CLEAR_REFERENCE,
        "A limit this user set on SOMEBODY ELSE keeps limiting them; only "
        "the name of who set it goes.",
    ),

    # -- append-only by grant ------------------------------------------- #
    ErasureRule(
        "audit_log", "actor_user_id", Disposition.RETAIN,
        "The security log is append-only for the application by design. "
        "Its read model shows the actor only as a stable pseudonym, so "
        "releasing the foreign key leaves who-did-what intact while "
        "who-that-was becomes unresolvable: the directory row it resolved "
        "against is gone.",
    ),
    ErasureRule(
        "llm_usage", "user_id", Disposition.RETAIN,
        "The cost ledger is append-only for the application and carries no "
        "pseudonym column, so it keeps the raw id until its own retention "
        "window closes.",
    ),
)


PLANNED_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    (rule.table, rule.column) for rule in ERASURE_PLAN
)


#: The tenant's deleted-account placeholder: the actor that REASSIGN rules
#: point at. It is an ordinary ``users`` row so every foreign key and
#: CHECK holds, but it is disabled from birth, has no credential, can never
#: sign in, and the admin listing hides it. One per tenant, created on
#: first use.
DELETED_ACCOUNT_ISSUER = "inqtrix:system"
DELETED_ACCOUNT_SUBJECT = "deleted-account"
DELETED_ACCOUNT_EMAIL = "deleted-account@invalid"
DELETED_ACCOUNT_DISPLAY_NAME = "Deleted account"


def rule_for(table: str, column: str) -> ErasureRule | None:
    """The plan's rule for one column, or ``None`` when it has none."""
    for rule in ERASURE_PLAN:
        if rule.table == table and rule.column == column:
            return rule
    return None


def rules_with(disposition: Disposition) -> tuple[ErasureRule, ...]:
    """Every rule of one disposition, in plan order."""
    return tuple(
        rule for rule in ERASURE_PLAN if rule.disposition is disposition
    )


def delegated_aggregates() -> tuple[str, ...]:
    """Aggregate names in plan order, each named once."""
    seen: list[str] = []
    for rule in rules_with(Disposition.DELEGATE):
        if rule.aggregate and rule.aggregate not in seen:
            seen.append(rule.aggregate)
    return tuple(seen)

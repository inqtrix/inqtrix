"""Let an account be erased without losing the security log.

Every foreign key into `users` is ON DELETE RESTRICT on purpose, so that
no cascade can quietly shred shared work. For one of them that choice has
a different consequence: `audit_log` is append-only for the application
role (INSERT/SELECT only, the one sanctioned deletion door is the
time-based `audit_prune`). The application therefore cannot clear the
audit rows that block a delete, and no account can ever be removed —
whatever else is cleaned up first.

The schema already carries the answer. `actor_user_id` is nullable and
exists only for the key and one index; the read model publishes the actor
ONLY as `actor_pseudonym`, a stable HMAC written beside it. Releasing the
key therefore changes nothing an auditor sees: who-did-what stays intact
and filterable, while who-that-was becomes unresolvable because the
directory row it resolved against is gone. The referential action runs
inside the database, so the application needs no new privilege on a table
it is deliberately not allowed to write.

That only holds where the pseudonym is actually present. Two writers
inserted their rows directly instead of through the one call that builds
the envelope, and left rows carrying an actor with no pseudonym — the
whole user-lifecycle family (logins admitted, accounts created, disabled
and enabled, passwords reset) plus the workspace family. Nulling their
actor would not de-identify those rows, it would erase the actor outright
and the admin panel would keep showing them as anonymous forever. The
backfill below closes that gap first, and it is ordered before the key
change for exactly that reason.

The pseudonym is an HMAC over an instance pepper, so it cannot be
recomputed in SQL. It is copied from any row that already has one for the
same actor — the pseudonym is stable per person by construction, which is
what makes the audit trail joinable at all. Rows whose actor never wrote
a pseudonymised row anywhere keep their actor id: this migration does not
invent an attribution it cannot derive, and the key change leaves those
rows' actors NULL only when their user is actually erased.
"""

import sqlalchemy as sa
from alembic import op

revision = "0088_user_erasure"
down_revision = "0087_comment_thread_deletion"
branch_labels = None
depends_on = None

_TABLE = "audit_log"
_CONSTRAINT = "fk_audit_log_actor_user_id_users"


def upgrade() -> None:
    # 1. Give every attributable row its pseudonym, copied from a row the
    #    same actor already wrote through the complete path.
    op.execute(
        sa.text(
            """
            UPDATE audit_log AS target
               SET actor_pseudonym = source.actor_pseudonym
              FROM (
                    SELECT DISTINCT ON (actor_user_id)
                           actor_user_id, actor_pseudonym
                      FROM audit_log
                     WHERE actor_user_id IS NOT NULL
                       AND actor_pseudonym IS NOT NULL
                       AND actor_pseudonym <> ''
                     ORDER BY actor_user_id, id DESC
                   ) AS source
             WHERE target.actor_user_id = source.actor_user_id
               AND (target.actor_pseudonym IS NULL
                    OR target.actor_pseudonym = '')
            """
        )
    )

    # 2. Release the key so deleting the directory row de-identifies the
    #    history instead of being refused by it.
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="foreignkey")
    op.create_foreign_key(
        _CONSTRAINT,
        _TABLE,
        "users",
        ["actor_user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # The backfill is not undone: a pseudonym that is present is correct,
    # and removing it would take attribution away from rows that have it.
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="foreignkey")
    op.create_foreign_key(
        _CONSTRAINT,
        _TABLE,
        "users",
        ["actor_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )

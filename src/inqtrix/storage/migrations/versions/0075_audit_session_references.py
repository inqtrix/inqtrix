"""Remove former browser-session credentials from the durable audit trail.

New logout writers persist a keyed, domain-separated ``ses_<hex16>``
reference. Historical rows predate that contract and contain the full
256-bit browser-session credential. This revision replaces those values
inside PostgreSQL so they never cross the migration-process boundary.

The historical derivation is deliberately domain-separated and deterministic:
duplicate legacy rows for one session remain correlatable. Session identifiers
have 256 bits of cryptographic entropy, so SHA-256 has no low-entropy input
domain to enumerate. Already-safe references and every non-logout row remain
byte-identical.

Revision ID: 0075_audit_session_references
Revises: 0074_llm_usage_run_index
"""

from __future__ import annotations

from alembic import op

revision = "0075_audit_session_references"
down_revision = "0074_llm_usage_run_index"
branch_labels = None
depends_on = None

_SAFE_REFERENCE_PATTERN = r"^ses_[0-9a-f]{16}$"

_LOCK_SQL = "LOCK TABLE audit_log IN ACCESS EXCLUSIVE MODE"

_SANITIZE_SQL = f"""
UPDATE audit_log
SET resource_id = 'ses_' || substr(
    encode(
        sha256(
            convert_to(
                'inqtrix.audit.session.v1:' || resource_id,
                'UTF8'
            )
        ),
        'hex'
    ),
    1,
    16
)
WHERE action = 'auth.logout'
  AND resource_type = 'session'
  AND resource_id !~ '{_SAFE_REFERENCE_PATTERN}'
"""

_POSTCONDITION_SQL = f"""
DO $$
DECLARE unsafe_count bigint;
BEGIN
    SELECT count(*) INTO unsafe_count
    FROM audit_log
    WHERE action = 'auth.logout'
      AND resource_type = 'session'
      AND resource_id !~ '{_SAFE_REFERENCE_PATTERN}';

    IF unsafe_count <> 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'Audit session sanitization left ' ||
                      unsafe_count || ' unsafe logout row(s).';
    END IF;
END
$$
"""


def upgrade() -> None:
    op.execute(_LOCK_SQL)
    op.execute(_SANITIZE_SQL)
    op.execute(_POSTCONDITION_SQL)


def downgrade() -> None:
    raise RuntimeError(
        "This migration is irreversible: former browser-session credentials "
        "were deliberately destroyed. Restore the matching pre-upgrade "
        "backup instead."
    )

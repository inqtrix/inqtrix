"""Replace external-subject authority with canonical user UUIDs.

Revision ID: 0045_canonical_user_ids
Revises: 0044_agent_task_cancellation

This is the irreversible v0.2 identity cut. Login provenance remains on
``users`` and the login/session bindings, but authorization, ownership, quota,
sharing, and audit relations reference ``users.id`` exclusively. The upgrade
locks every affected table before repeating its security-critical preflights;
no subject is guessed when the legacy schema omitted its issuer.
"""

from __future__ import annotations

from alembic import op

revision = "0045_canonical_user_ids"
down_revision = "0044_agent_task_cancellation"
branch_labels = None
depends_on = None

_APP_ROLE = "inqtrix_app"
_DEFAULT_QUOTA_SUBJECT = "__quota_default__"


class _AuthorityColumn:
    """One legacy bare-subject authority column and its UUID replacement."""

    __slots__ = ("table", "legacy", "canonical", "nullable", "predicate")

    def __init__(
        self,
        table: str,
        legacy: str,
        canonical: str,
        nullable: bool,
        predicate: str = "TRUE",
    ) -> None:
        self.table = table
        self.legacy = legacy
        self.canonical = canonical
        self.nullable = nullable
        self.predicate = predicate


_AUTHORITY_COLUMNS: tuple[_AuthorityColumn, ...] = (
    _AuthorityColumn("workspaces", "created_by_sub", "created_by_user_id", False),
    _AuthorityColumn("workspace_members", "sub", "user_id", False),
    _AuthorityColumn(
        "invitations", "invited_by_sub", "invited_by_user_id", False
    ),
    _AuthorityColumn(
        "invitations", "accepted_by_sub", "accepted_by_user_id", True
    ),
    _AuthorityColumn("audit_log", "actor_sub", "actor_user_id", True),
    _AuthorityColumn("account_preferences", "sub", "user_id", False),
    _AuthorityColumn(
        "quota_usage_counters", "subject_sub", "subject_user_id", False
    ),
    _AuthorityColumn(
        "quota_limits",
        "subject_sub",
        "subject_user_id",
        True,
        f"t.subject_sub <> '{_DEFAULT_QUOTA_SUBJECT}'",
    ),
    _AuthorityColumn("quota_limits", "set_by_sub", "set_by_user_id", True),
    _AuthorityColumn("files", "owner_sub", "owner_user_id", True),
    _AuthorityColumn("runs", "created_by_sub", "created_by_user_id", True),
    _AuthorityColumn(
        "indexing_jobs", "created_by_sub", "created_by_user_id", True
    ),
    _AuthorityColumn(
        "knowledge_collections", "created_by_sub", "created_by_user_id", True
    ),
    _AuthorityColumn(
        "prompt_templates", "owner_sub", "owner_user_id", True
    ),
    _AuthorityColumn("skill_templates", "owner_sub", "owner_user_id", True),
    _AuthorityColumn(
        "chat_thread_groups", "created_by_sub", "created_by_user_id", True
    ),
    _AuthorityColumn("chat_threads", "created_by_sub", "created_by_user_id", True),
    _AuthorityColumn(
        "editor_folders", "created_by_sub", "created_by_user_id", True
    ),
    _AuthorityColumn(
        "editor_documents", "created_by_sub", "created_by_user_id", True
    ),
    _AuthorityColumn(
        "editor_patches", "created_by_sub", "created_by_user_id", True
    ),
    _AuthorityColumn(
        "asset_sections", "created_by_sub", "created_by_user_id", True
    ),
    _AuthorityColumn("asset_groups", "created_by_sub", "created_by_user_id", True),
    _AuthorityColumn(
        "asset_records", "created_by_sub", "created_by_user_id", True
    ),
    _AuthorityColumn(
        "knowledge_session_groups",
        "created_by_sub",
        "created_by_user_id",
        True,
    ),
    _AuthorityColumn(
        "knowledge_sessions", "created_by_sub", "created_by_user_id", True
    ),
    _AuthorityColumn(
        "agent_session_groups", "created_by_sub", "created_by_user_id", True
    ),
    _AuthorityColumn(
        "agent_sessions", "created_by_sub", "created_by_user_id", True
    ),
    _AuthorityColumn(
        "vector_index_records", "created_by_sub", "created_by_user_id", True
    ),
    _AuthorityColumn("agent_memory_candidates", "sub", "user_id", False),
    _AuthorityColumn("agent_feedback", "sub", "user_id", False),
    _AuthorityColumn(
        "run_approvals", "decided_by_sub", "decided_by_user_id", True
    ),
    _AuthorityColumn(
        "run_clarifications", "answered_by_sub", "answered_by_user_id", True
    ),
)

_EXACT_AUTHORITY_TABLES = (
    "auth_sessions",
    "personal_access_tokens",
    "local_credentials",
)

_REQUIRED_TABLES = tuple(
    dict.fromkeys(
        (
            "users",
            "groups",
            "group_members",
            "resource_shares",
            "runs",
            "knowledge_collections",
            "prompt_templates",
            "skill_templates",
            *[spec.table for spec in _AUTHORITY_COLUMNS],
            *_EXACT_AUTHORITY_TABLES,
        )
    )
)

_LOCK_TABLES = ", ".join(f'"{table}"' for table in _REQUIRED_TABLES)

_RESOURCE_TYPES = (
    "'run', 'knowledge_collection', 'prompt_template', 'skill_template'"
)

_SHARE_RESOURCE_TABLES: dict[str, tuple[str, str]] = {
    "run": ("runs", "run_id"),
    "knowledge_collection": ("knowledge_collections", "id"),
    "prompt_template": ("prompt_templates", "id"),
    "skill_template": ("skill_templates", "id"),
}

_SHARE_RESOURCE_OWNER_COLUMNS: dict[str, str] = {
    "run": "created_by_user_id",
    "knowledge_collection": "created_by_user_id",
    "prompt_template": "owner_user_id",
    "skill_template": "owner_user_id",
}

_RESOURCE_EXISTENCE = """
(s.resource_type = 'run' AND NOT EXISTS (
    SELECT 1 FROM runs AS r
    WHERE r.tenant_id = s.tenant_id AND r.run_id::text = s.resource_id
)) OR
(s.resource_type = 'knowledge_collection' AND NOT EXISTS (
    SELECT 1 FROM knowledge_collections AS r
    WHERE r.tenant_id = s.tenant_id AND r.id::text = s.resource_id
)) OR
(s.resource_type = 'prompt_template' AND NOT EXISTS (
    SELECT 1 FROM prompt_templates AS r
    WHERE r.tenant_id = s.tenant_id AND r.id::text = s.resource_id
)) OR
(s.resource_type = 'skill_template' AND NOT EXISTS (
    SELECT 1 FROM skill_templates AS r
    WHERE r.tenant_id = s.tenant_id AND r.id::text = s.resource_id
))
"""

_RESOURCE_OWNERLESS = " OR\n".join(
    f"""(s.resource_type = '{resource_type}' AND EXISTS (
    SELECT 1 FROM {table} AS r
    WHERE r.tenant_id = s.tenant_id
      AND r.{id_column}::text = s.resource_id
      AND r.{_SHARE_RESOURCE_OWNER_COLUMNS[resource_type]} IS NULL
))"""
    for resource_type, (table, id_column) in _SHARE_RESOURCE_TABLES.items()
)


def _column_exists_sql(table: str, column: str) -> str:
    """Return a catalog predicate for one column in the active schema."""
    return (
        "EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        f"AND table_name = '{table}' AND column_name = '{column}')"
    )


def _bare_authority_preflight_sql(spec: _AuthorityColumn) -> str:
    """Build the locked zero/ambiguous/canonical check for one column."""
    legacy_exists = _column_exists_sql(spec.table, spec.legacy)
    canonical_exists = _column_exists_sql(spec.table, spec.canonical)
    return f"""
DO $migration$
DECLARE
    legacy_exists boolean := {legacy_exists};
    canonical_exists boolean := {canonical_exists};
    canonical_type text;
    orphaned bigint := 0;
    ambiguous bigint := 0;
    invalid_canonical bigint := 0;
    mismatched bigint := 0;
BEGIN
    IF NOT legacy_exists AND NOT canonical_exists THEN
        RAISE EXCEPTION
            '0045 requires {spec.table}.{spec.legacy} or {spec.canonical}';
    END IF;

    IF canonical_exists THEN
        SELECT udt_name INTO canonical_type
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = '{spec.table}'
          AND column_name = '{spec.canonical}';
        IF canonical_type NOT IN ('uuid', 'text') THEN
            RAISE EXCEPTION
                '0045 incompatible type for {spec.table}.{spec.canonical}: %',
                canonical_type;
        END IF;

        SELECT count(*) INTO invalid_canonical
        FROM {spec.table} AS t
        LEFT JOIN users AS u
          ON u.tenant_id = t.tenant_id
         AND u.id::text = t.{spec.canonical}::text
        WHERE t.{spec.canonical} IS NOT NULL
          AND u.id IS NULL;
        IF invalid_canonical > 0 THEN
            RAISE EXCEPTION
                '0045 found % invalid canonical references in '
                '{spec.table}.{spec.canonical}', invalid_canonical;
        END IF;
    END IF;

    IF legacy_exists THEN
        SELECT
            count(*) FILTER (WHERE m.match_count IS NULL),
            count(*) FILTER (WHERE m.match_count > 1)
        INTO orphaned, ambiguous
        FROM {spec.table} AS t
        LEFT JOIN _inqtrix_user_subject_map AS m
          ON m.tenant_id = t.tenant_id
         AND m.subject = t.{spec.legacy}
        WHERE t.{spec.legacy} IS NOT NULL
          AND ({spec.predicate});

        IF orphaned > 0 THEN
            RAISE EXCEPTION
                '0045 cannot map % {spec.table}.{spec.legacy} values: '
                'no (tenant_id, issuer, subject) user match', orphaned;
        END IF;
        IF ambiguous > 0 THEN
            RAISE EXCEPTION
                '0045 cannot map % {spec.table}.{spec.legacy} values: '
                'more than one issuer-scoped user match', ambiguous;
        END IF;

        IF canonical_exists THEN
            SELECT count(*) INTO mismatched
            FROM {spec.table} AS t
            JOIN _inqtrix_user_subject_map AS m
              ON m.tenant_id = t.tenant_id
             AND m.subject = t.{spec.legacy}
             AND m.match_count = 1
            WHERE t.{spec.legacy} IS NOT NULL
              AND ({spec.predicate})
              AND t.{spec.canonical} IS NOT NULL
              AND t.{spec.canonical}::text <> m.user_id::text;
            IF mismatched > 0 THEN
                RAISE EXCEPTION
                    '0045 found % conflicting values in '
                    '{spec.table}.{spec.canonical}', mismatched;
            END IF;
        END IF;
    END IF;
END
$migration$;
"""


def _prepare_bare_authority_sql(spec: _AuthorityColumn) -> tuple[str, ...]:
    """Build the UUID add/type-conversion/backfill for one checked column."""
    statements = [
        f"ALTER TABLE {spec.table} "
        f"ADD COLUMN IF NOT EXISTS {spec.canonical} uuid NULL",
        f"""
DO $migration$
DECLARE
    canonical_type text;
BEGIN
    SELECT udt_name INTO canonical_type
    FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = '{spec.table}'
      AND column_name = '{spec.canonical}';
    IF canonical_type <> 'uuid' THEN
        EXECUTE 'ALTER TABLE {spec.table} ALTER COLUMN {spec.canonical} '
                'TYPE uuid USING {spec.canonical}::text::uuid';
    END IF;
    IF {_column_exists_sql(spec.table, spec.legacy)} THEN
        EXECUTE $update$
            UPDATE {spec.table} AS t
            SET {spec.canonical} = m.user_id
            FROM _inqtrix_user_subject_map AS m
            WHERE m.tenant_id = t.tenant_id
              AND m.subject = t.{spec.legacy}
              AND m.match_count = 1
              AND t.{spec.legacy} IS NOT NULL
              AND ({spec.predicate})
        $update$;
    END IF;
END
$migration$;
""",
    ]
    if not spec.nullable:
        statements.append(
            f"ALTER TABLE {spec.table} "
            f"ALTER COLUMN {spec.canonical} SET NOT NULL"
        )
    return tuple(statements)


def _replace_primary_key_sql(
    *, table: str, legacy_column: str, constraint: str, columns: str
) -> str:
    """Replace a legacy primary key only when its old column exists."""
    return f"""
DO $migration$
BEGIN
    IF {_column_exists_sql(table, legacy_column)} THEN
        ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint};
        ALTER TABLE {table} ADD CONSTRAINT {constraint} PRIMARY KEY ({columns});
    END IF;
END
$migration$;
"""


def _foreign_key_sql(table: str, column: str) -> str:
    """Build an idempotent single-column users FK with delete restriction."""
    constraint = f"fk_{table}_{column}_users"
    return f"""
DO $migration$
DECLARE
    authority_attnum smallint;
BEGIN
    SELECT attnum INTO authority_attnum
    FROM pg_attribute
    WHERE attrelid = '{table}'::regclass
      AND attname = '{column}'
      AND NOT attisdropped;
    IF authority_attnum IS NULL THEN
        RAISE EXCEPTION '0045 missing authority column {table}.{column}';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = '{table}'::regclass
          AND confrelid = 'users'::regclass
          AND contype = 'f'
          AND conkey = ARRAY[authority_attnum]::smallint[]
          AND confdeltype <> 'r'
    ) THEN
        RAISE EXCEPTION
            '0045 found non-RESTRICT users FK on {table}.{column}';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = '{table}'::regclass
          AND confrelid = 'users'::regclass
          AND contype = 'f'
          AND conkey = ARRAY[authority_attnum]::smallint[]
          AND confdeltype = 'r'
    ) THEN
        ALTER TABLE {table}
            ADD CONSTRAINT {constraint}
            FOREIGN KEY ({column}) REFERENCES users(id) ON DELETE RESTRICT;
    END IF;
END
$migration$;
"""


def _drop_legacy_authority_sql(spec: _AuthorityColumn) -> str:
    """Drop one external-subject authority column after its UUID cutover."""
    return (
        f"ALTER TABLE {spec.table} "
        f"DROP COLUMN IF EXISTS {spec.legacy}"
    )


_SHARE_AUTHORITY_COLUMNS: tuple[_AuthorityColumn, ...] = (
    _AuthorityColumn(
        "resource_shares",
        "subject_id",
        "recipient_user_id",
        True,
        "t.subject_type = 'user' "
        f"AND t.resource_type IN ({_RESOURCE_TYPES}) "
        "AND t.permission IN ('view', 'edit')",
    ),
    _AuthorityColumn(
        "resource_shares",
        "granted_by_sub",
        "granted_by_user_id",
        True,
        "t.subject_type = 'user' "
        f"AND t.resource_type IN ({_RESOURCE_TYPES}) "
        "AND t.permission IN ('view', 'edit')",
    ),
    _AuthorityColumn(
        "resource_shares",
        "revoked_by_sub",
        "revoked_by_user_id",
        True,
        "t.subject_type = 'user' "
        f"AND t.resource_type IN ({_RESOURCE_TYPES}) "
        "AND t.permission IN ('view', 'edit')",
    ),
)


def _required_schema_preflight_sql() -> str:
    """Build the real-0044-table and canonical-user uniqueness preflight."""
    required = ", ".join(f"'{table}'" for table in _REQUIRED_TABLES)
    return f"""
DO $migration$
DECLARE
    missing_tables text;
    duplicate_identities bigint;
BEGIN
    SELECT string_agg(required_table, ', ' ORDER BY required_table)
    INTO missing_tables
    FROM unnest(ARRAY[{required}]) AS required_table
    WHERE to_regclass(
        format('%I.%I', current_schema(), required_table)
    ) IS NULL;
    IF missing_tables IS NOT NULL THEN
        RAISE EXCEPTION
            '0045 requires the complete 0044 schema; missing tables: %',
            missing_tables;
    END IF;

    SELECT count(*) INTO duplicate_identities
    FROM (
        SELECT tenant_id, issuer, subject
        FROM users
        GROUP BY tenant_id, issuer, subject
        HAVING count(*) > 1
    ) AS duplicates;
    IF duplicate_identities > 0 THEN
        RAISE EXCEPTION
            '0045 found % duplicate (tenant_id, issuer, subject) identities',
            duplicate_identities;
    END IF;
END
$migration$;
"""


_WORK_AND_SHARE_PREFLIGHT_SQL = f"""
DO $migration$
DECLARE
    nonterminal_runs bigint;
    nonterminal_reindex_jobs bigint;
    unsupported_active_shares bigint := 0;
    legacy_shares boolean := {_column_exists_sql('resource_shares', 'subject_type')};
    share_revision_type text;
BEGIN
    SELECT count(*) INTO nonterminal_runs
    FROM runs
    WHERE status NOT IN ('completed', 'failed', 'cancelled');
    IF nonterminal_runs > 0 THEN
        RAISE EXCEPTION
            '0045 refuses % non-terminal runs; terminate them explicitly',
            nonterminal_runs;
    END IF;

    SELECT count(*) INTO nonterminal_reindex_jobs
    FROM indexing_jobs
    WHERE status NOT IN ('completed', 'failed', 'cancelled');
    IF nonterminal_reindex_jobs > 0 THEN
        RAISE EXCEPTION
            '0045 refuses % non-terminal reindex jobs; terminate them explicitly',
            nonterminal_reindex_jobs;
    END IF;

    IF legacy_shares THEN
        EXECUTE $query$
            SELECT count(*)
            FROM resource_shares
            WHERE revoked_at IS NULL
              AND (
                  subject_type <> 'user'
                  OR resource_type NOT IN ({_RESOURCE_TYPES})
                  OR permission NOT IN ('view', 'edit')
              )
        $query$ INTO unsupported_active_shares;
    ELSE
        SELECT count(*) INTO unsupported_active_shares
        FROM resource_shares
        WHERE revoked_at IS NULL
          AND (
              resource_type NOT IN ({_RESOURCE_TYPES})
              OR permission NOT IN ('view', 'edit')
          );
    END IF;
    IF unsupported_active_shares > 0 THEN
        RAISE EXCEPTION
            '0045 refuses % active group/file/comment/manage or otherwise '
            'unsupported shares; revoke them explicitly',
            unsupported_active_shares;
    END IF;

    IF {_column_exists_sql('resource_shares', 'revision')} THEN
        SELECT udt_name INTO share_revision_type
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'resource_shares'
          AND column_name = 'revision';
        IF share_revision_type <> 'int8' THEN
            RAISE EXCEPTION
                '0045 incompatible resource_shares.revision type: %',
                share_revision_type;
        END IF;
    END IF;
END
$migration$;
"""


_AUTH_SESSION_PREFLIGHT_SQL = f"""
DO $migration$
DECLARE
    legacy_exists boolean := {_column_exists_sql('auth_sessions', 'sub')};
    subject_exists boolean := {_column_exists_sql('auth_sessions', 'subject')};
    canonical_exists boolean := {_column_exists_sql('auth_sessions', 'user_id')};
    orphaned bigint := 0;
    ambiguous bigint := 0;
    invalid bigint := 0;
    conflicting_subject bigint := 0;
BEGIN
    IF NOT legacy_exists AND NOT subject_exists THEN
        RAISE EXCEPTION '0045 missing auth_sessions login subject';
    END IF;
    IF legacy_exists THEN
        EXECUTE $query$
            SELECT
                count(*) FILTER (WHERE matches = 0),
                count(*) FILTER (WHERE matches > 1)
            FROM (
                SELECT (
                    SELECT count(*) FROM users AS u
                    WHERE u.tenant_id = s.tenant_id
                      AND u.issuer = s.issuer
                      AND u.subject = s.sub
                ) AS matches
                FROM auth_sessions AS s
            ) AS refs
        $query$ INTO orphaned, ambiguous;
        IF orphaned > 0 OR ambiguous > 0 THEN
            RAISE EXCEPTION
                '0045 cannot map auth_sessions by '
                '(tenant_id, issuer, subject): orphaned=%, ambiguous=%',
                orphaned, ambiguous;
        END IF;
        IF subject_exists THEN
            EXECUTE $query$
                SELECT count(*) FROM auth_sessions
                WHERE subject IS NOT NULL AND subject IS DISTINCT FROM sub
            $query$ INTO conflicting_subject;
            IF conflicting_subject > 0 THEN
                RAISE EXCEPTION
                    '0045 found % conflicting auth session legacy/canonical '
                    'subjects', conflicting_subject;
            END IF;
        END IF;
    END IF;
    IF canonical_exists THEN
        IF subject_exists THEN
            SELECT count(*) INTO invalid
            FROM auth_sessions AS s
            LEFT JOIN users AS u
              ON u.id::text = s.user_id::text
             AND u.tenant_id = s.tenant_id
             AND u.issuer = s.issuer
             AND u.subject = s.subject
            WHERE s.user_id IS NOT NULL AND u.id IS NULL;
        ELSE
            EXECUTE $query$
                SELECT count(*)
                FROM auth_sessions AS s
                LEFT JOIN users AS u
                  ON u.id::text = s.user_id::text
                 AND u.tenant_id = s.tenant_id
                 AND u.issuer = s.issuer
                 AND u.subject = s.sub
                WHERE s.user_id IS NOT NULL AND u.id IS NULL
            $query$ INTO invalid;
        END IF;
        IF invalid > 0 THEN
            RAISE EXCEPTION
                '0045 found % invalid auth_sessions.user_id bindings', invalid;
        END IF;
    END IF;
END
$migration$;
"""


_PAT_OWNER_SUB_EXISTS = _column_exists_sql(
    "personal_access_tokens", "owner_sub"
)
_PAT_OWNER_USER_ID_EXISTS = _column_exists_sql(
    "personal_access_tokens", "owner_user_id"
)

_PAT_PREFLIGHT_SQL = f"""
DO $migration$
DECLARE
    legacy_exists boolean := {_PAT_OWNER_SUB_EXISTS};
    canonical_exists boolean := {_PAT_OWNER_USER_ID_EXISTS};
    orphaned bigint := 0;
    ambiguous bigint := 0;
    invalid bigint := 0;
    conflicting bigint := 0;
BEGIN
    IF NOT legacy_exists AND NOT canonical_exists THEN
        RAISE EXCEPTION '0045 missing PAT owner authority';
    END IF;
    IF legacy_exists THEN
        SELECT
            count(*) FILTER (WHERE matches = 0),
            count(*) FILTER (WHERE matches > 1)
        INTO orphaned, ambiguous
        FROM (
            SELECT (
                SELECT count(*) FROM users AS u
                WHERE u.tenant_id = p.tenant_id
                  AND u.issuer = p.owner_issuer
                  AND u.subject = p.owner_sub
            ) AS matches
            FROM personal_access_tokens AS p
        ) AS refs;
        IF orphaned > 0 OR ambiguous > 0 THEN
            RAISE EXCEPTION
                '0045 cannot map PAT owners by '
                '(tenant_id, issuer, subject): orphaned=%, ambiguous=%',
                orphaned, ambiguous;
        END IF;
        IF canonical_exists THEN
            SELECT count(*) INTO conflicting
            FROM personal_access_tokens AS p
            JOIN users AS u
              ON u.tenant_id = p.tenant_id
             AND u.issuer = p.owner_issuer
             AND u.subject = p.owner_sub
            WHERE p.owner_user_id IS NOT NULL
              AND p.owner_user_id::text <> u.id::text;
            IF conflicting > 0 THEN
                RAISE EXCEPTION
                    '0045 found % conflicting PAT legacy/canonical owners',
                    conflicting;
            END IF;
        END IF;
    END IF;
    IF canonical_exists THEN
        SELECT count(*) INTO invalid
        FROM personal_access_tokens AS p
        LEFT JOIN users AS u
          ON u.id::text = p.owner_user_id::text
         AND u.tenant_id = p.tenant_id
        WHERE p.owner_user_id IS NOT NULL AND u.id IS NULL;
        IF invalid > 0 THEN
            RAISE EXCEPTION
                '0045 found % invalid PAT owner_user_id values', invalid;
        END IF;
    END IF;
END
$migration$;
"""


_CREDENTIAL_PREFLIGHT_SQL = f"""
DO $migration$
DECLARE
    canonical_exists boolean := {_column_exists_sql('local_credentials', 'user_id')};
    orphaned bigint;
    ambiguous bigint;
    invalid bigint := 0;
BEGIN
    SELECT
        count(*) FILTER (WHERE matches = 0),
        count(*) FILTER (WHERE matches > 1)
    INTO orphaned, ambiguous
    FROM (
        SELECT (
            SELECT count(*) FROM users AS u
            WHERE u.tenant_id = c.tenant_id
              AND u.issuer = 'local'
              AND u.subject = c.subject
        ) AS matches
        FROM local_credentials AS c
    ) AS refs;
    IF orphaned > 0 OR ambiguous > 0 THEN
        RAISE EXCEPTION
            '0045 cannot map local credentials by '
            '(tenant_id, local, subject): orphaned=%, ambiguous=%',
            orphaned, ambiguous;
    END IF;
    IF canonical_exists THEN
        SELECT count(*) INTO invalid
        FROM local_credentials AS c
        LEFT JOIN users AS u
          ON u.id::text = c.user_id::text
         AND u.tenant_id = c.tenant_id
         AND u.issuer = 'local'
         AND u.subject = c.subject
        WHERE c.user_id IS NOT NULL AND u.id IS NULL;
        IF invalid > 0 THEN
            RAISE EXCEPTION
                '0045 found % invalid local credential user bindings', invalid;
        END IF;
    END IF;
END
$migration$;
"""


_QUOTA_DEFAULT_PREFLIGHT_SQL = f"""
DO $migration$
DECLARE
    legacy_exists boolean := {_column_exists_sql('quota_limits', 'subject_sub')};
    canonical_exists boolean := {_column_exists_sql('quota_limits', 'subject_user_id')};
    invalid bigint := 0;
BEGIN
    IF legacy_exists AND canonical_exists THEN
        EXECUTE $query$
            SELECT count(*) FROM quota_limits
            WHERE subject_sub = '{_DEFAULT_QUOTA_SUBJECT}'
              AND subject_user_id IS NOT NULL
        $query$ INTO invalid;
        IF invalid > 0 THEN
            RAISE EXCEPTION
                '0045 found % quota-default rows with a user authority', invalid;
        END IF;
    END IF;
END
$migration$;
"""


_PREPARE_AUTH_SESSION_SQL = (
    "ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS user_id uuid NULL",
    "ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS subject text NULL",
    f"""
DO $migration$
DECLARE
    canonical_type text;
BEGIN
    SELECT udt_name INTO canonical_type
    FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'auth_sessions'
      AND column_name = 'user_id';
    IF canonical_type <> 'uuid' THEN
        ALTER TABLE auth_sessions
            ALTER COLUMN user_id TYPE uuid USING user_id::text::uuid;
    END IF;
    IF {_column_exists_sql('auth_sessions', 'sub')} THEN
        EXECUTE $update$
            UPDATE auth_sessions SET subject = sub
            WHERE subject IS NULL OR subject = sub
        $update$;
    END IF;
    UPDATE auth_sessions AS s
    SET user_id = u.id
    FROM users AS u
    WHERE u.tenant_id = s.tenant_id
      AND u.issuer = s.issuer
      AND u.subject = s.subject;
END
$migration$;
""",
    "ALTER TABLE auth_sessions ALTER COLUMN user_id SET NOT NULL",
    "ALTER TABLE auth_sessions ALTER COLUMN subject SET NOT NULL",
)


_PREPARE_PAT_SQL = (
    "ALTER TABLE personal_access_tokens "
    "ADD COLUMN IF NOT EXISTS owner_user_id uuid NULL",
    f"""
DO $migration$
DECLARE
    canonical_type text;
BEGIN
    SELECT udt_name INTO canonical_type
    FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'personal_access_tokens'
      AND column_name = 'owner_user_id';
    IF canonical_type <> 'uuid' THEN
        ALTER TABLE personal_access_tokens
            ALTER COLUMN owner_user_id TYPE uuid
            USING owner_user_id::text::uuid;
    END IF;
    IF {_column_exists_sql('personal_access_tokens', 'owner_sub')} THEN
        EXECUTE $update$
            UPDATE personal_access_tokens AS p
            SET owner_user_id = u.id
            FROM users AS u
            WHERE u.tenant_id = p.tenant_id
              AND u.issuer = p.owner_issuer
              AND u.subject = p.owner_sub
        $update$;
    END IF;
END
$migration$;
""",
    "ALTER TABLE personal_access_tokens "
    "ALTER COLUMN owner_user_id SET NOT NULL",
)


_PREPARE_CREDENTIAL_SQL = (
    "ALTER TABLE local_credentials ADD COLUMN IF NOT EXISTS user_id uuid NULL",
    """
DO $migration$
DECLARE
    canonical_type text;
BEGIN
    SELECT udt_name INTO canonical_type
    FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'local_credentials'
      AND column_name = 'user_id';
    IF canonical_type <> 'uuid' THEN
        ALTER TABLE local_credentials
            ALTER COLUMN user_id TYPE uuid USING user_id::text::uuid;
    END IF;
END
$migration$;
""",
    """
UPDATE local_credentials AS c
SET user_id = u.id
FROM users AS u
WHERE u.tenant_id = c.tenant_id
  AND u.issuer = 'local'
  AND u.subject = c.subject
""",
    "ALTER TABLE local_credentials ALTER COLUMN user_id SET NOT NULL",
)


_USER_CONSTRAINT_SQL = (
    "ALTER TABLE users DROP CONSTRAINT IF EXISTS uq_users_issuer_subject",
    """
DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'users'::regclass
          AND conname = 'uq_users_tenant_issuer_subject'
    ) THEN
        ALTER TABLE users
            ADD CONSTRAINT uq_users_tenant_issuer_subject
            UNIQUE (tenant_id, issuer, subject);
    END IF;
END
$migration$;
""",
)


_TENANT_SECURITY_STATE_SQL = (
    "CREATE TABLE IF NOT EXISTS tenant_security_state "
    "(tenant_id text PRIMARY KEY)",
    "ALTER TABLE tenant_security_state DROP COLUMN IF EXISTS created_at",
    """
INSERT INTO tenant_security_state (tenant_id)
SELECT DISTINCT tenant_id FROM users
ON CONFLICT (tenant_id) DO NOTHING
""",
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_security_state TO {_APP_ROLE}",
    "ALTER TABLE tenant_security_state ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE tenant_security_state FORCE ROW LEVEL SECURITY",
    """
DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = current_schema()
          AND tablename = 'tenant_security_state'
          AND policyname = 'tenant_isolation'
    ) THEN
        CREATE POLICY tenant_isolation ON tenant_security_state
            FOR ALL
            USING (tenant_id = (SELECT inqtrix_current_tenant_id()))
            WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()));
    END IF;
END
$migration$;
""",
)


_QUOTA_PRIMARY_KEY_SQL = (
    "ALTER TABLE quota_limits "
    "ADD COLUMN IF NOT EXISTS id uuid DEFAULT gen_random_uuid()",
    "ALTER TABLE quota_limits ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "UPDATE quota_limits SET id = gen_random_uuid() WHERE id IS NULL",
    "ALTER TABLE quota_limits ALTER COLUMN id SET NOT NULL",
    """
DO $migration$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'quota_limits'
          AND column_name = 'subject_sub'
    ) THEN
        ALTER TABLE quota_limits DROP CONSTRAINT IF EXISTS pk_quota_limits;
        ALTER TABLE quota_limits DROP CONSTRAINT IF EXISTS quota_limits_pkey;
        ALTER TABLE quota_limits
            ADD CONSTRAINT quota_limits_pkey PRIMARY KEY (id);
    ELSIF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'quota_limits'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE quota_limits
            ADD CONSTRAINT quota_limits_pkey PRIMARY KEY (id);
    END IF;
END
$migration$;
""",
)


_PRIMARY_KEY_MIGRATIONS = (
    _replace_primary_key_sql(
        table="workspace_members",
        legacy_column="sub",
        constraint="workspace_members_pkey",
        columns="workspace_id, user_id",
    ),
    _replace_primary_key_sql(
        table="account_preferences",
        legacy_column="sub",
        constraint="account_preferences_pkey",
        columns="tenant_id, user_id",
    ),
    _replace_primary_key_sql(
        table="quota_usage_counters",
        legacy_column="subject_sub",
        constraint="pk_quota_usage_counters",
        columns="tenant_id, subject_user_id, dimension, period_start",
    ),
    _replace_primary_key_sql(
        table="agent_memory_candidates",
        legacy_column="sub",
        constraint="pk_agent_memory_candidates",
        columns="tenant_id, user_id, candidate_id",
    ),
    _replace_primary_key_sql(
        table="agent_feedback",
        legacy_column="sub",
        constraint="pk_agent_feedback",
        columns="tenant_id, user_id, feedback_id",
    ),
    _replace_primary_key_sql(
        table="local_credentials",
        legacy_column="subject",
        constraint="local_credentials_pkey",
        columns="user_id",
    ),
)


_REVOKE_ORPHAN_SHARES_SQL = f"""
UPDATE resource_shares AS s
SET revoked_at = now()
WHERE s.revoked_at IS NULL
  AND (({_RESOURCE_EXISTENCE}) OR ({_RESOURCE_OWNERLESS}));
"""


_DELETE_UNSUPPORTED_SHARE_HISTORY_SQL = f"""
DO $migration$
BEGIN
    IF {_column_exists_sql('resource_shares', 'subject_type')} THEN
        EXECUTE $delete$
            DELETE FROM resource_shares
            WHERE revoked_at IS NOT NULL
              AND (
                  subject_type <> 'user'
                  OR resource_type NOT IN ({_RESOURCE_TYPES})
                  OR permission NOT IN ('view', 'edit')
              )
        $delete$;
    ELSE
        DELETE FROM resource_shares
        WHERE revoked_at IS NOT NULL
          AND (
              resource_type NOT IN ({_RESOURCE_TYPES})
              OR permission NOT IN ('view', 'edit')
          );
    END IF;
END
$migration$;
"""


_FINALIZE_SHARES_SQL = (
    "ALTER TABLE resource_shares ADD COLUMN IF NOT EXISTS "
    "revision bigint NOT NULL DEFAULT 1",
    "ALTER TABLE resource_shares ALTER COLUMN revision SET DEFAULT 1",
    "ALTER TABLE resource_shares ALTER COLUMN revision SET NOT NULL",
    "ALTER TABLE resource_shares DROP CONSTRAINT IF EXISTS "
    "ck_resource_shares_subject",
    "ALTER TABLE resource_shares DROP CONSTRAINT IF EXISTS "
    "ck_resource_shares_permission",
    "ALTER TABLE resource_shares DROP CONSTRAINT IF EXISTS "
    "ck_resource_shares_resource_type",
    "ALTER TABLE resource_shares DROP CONSTRAINT IF EXISTS "
    "ck_resource_shares_type",
    "DROP INDEX IF EXISTS uq_resource_shares_active",
    "DROP INDEX IF EXISTS ix_resource_shares_subject_active",
    "DROP INDEX IF EXISTS ix_resource_shares_recipient_active",
    "DROP INDEX IF EXISTS ix_resource_shares_resource_active",
    "ALTER TABLE resource_shares DROP COLUMN IF EXISTS subject_type",
    "ALTER TABLE resource_shares DROP COLUMN IF EXISTS subject_id",
    "ALTER TABLE resource_shares DROP COLUMN IF EXISTS granted_by_sub",
    "ALTER TABLE resource_shares DROP COLUMN IF EXISTS revoked_by_sub",
    "ALTER TABLE resource_shares ALTER COLUMN recipient_user_id SET NOT NULL",
    "ALTER TABLE resource_shares ALTER COLUMN granted_by_user_id SET NOT NULL",
    "ALTER TABLE resource_shares "
    "ADD CONSTRAINT ck_resource_shares_type "
    f"CHECK (resource_type IN ({_RESOURCE_TYPES}))",
    "ALTER TABLE resource_shares "
    "ADD CONSTRAINT ck_resource_shares_permission "
    "CHECK (permission IN ('view', 'edit'))",
    "CREATE UNIQUE INDEX uq_resource_shares_active ON resource_shares "
    "(tenant_id, recipient_user_id, resource_type, resource_id) "
    "WHERE revoked_at IS NULL",
    "CREATE INDEX ix_resource_shares_recipient_active ON resource_shares "
    "(tenant_id, recipient_user_id, resource_type) WHERE revoked_at IS NULL",
    "CREATE INDEX ix_resource_shares_resource_active ON resource_shares "
    "(tenant_id, resource_type, resource_id) WHERE revoked_at IS NULL",
)


_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_workspace_members_tenant_user "
    "ON workspace_members (tenant_id, user_id)",
    "CREATE INDEX IF NOT EXISTS ix_files_tenant_owner "
    "ON files (tenant_id, owner_user_id)",
    "CREATE INDEX IF NOT EXISTS ix_pat_owner "
    "ON personal_access_tokens (tenant_id, owner_user_id)",
    "CREATE INDEX IF NOT EXISTS ix_prompt_templates_owner "
    "ON prompt_templates (tenant_id, owner_user_id)",
    "CREATE INDEX IF NOT EXISTS ix_skill_templates_owner "
    "ON skill_templates (tenant_id, owner_user_id)",
    "CREATE INDEX IF NOT EXISTS ix_runs_user_id_active "
    "ON runs (created_by_user_id, status) "
    "WHERE status IN ('queued', 'running')",
    "CREATE INDEX IF NOT EXISTS ix_chat_thread_groups_owner_created "
    "ON chat_thread_groups (tenant_id, created_by_user_id, created_at, id)",
    "CREATE INDEX IF NOT EXISTS ix_chat_threads_owner_created "
    "ON chat_threads (tenant_id, created_by_user_id, created_at, id)",
    "CREATE INDEX IF NOT EXISTS ix_editor_folders_owner_created "
    "ON editor_folders (tenant_id, created_by_user_id, created_at, id)",
    "CREATE INDEX IF NOT EXISTS ix_editor_documents_owner_created "
    "ON editor_documents (tenant_id, created_by_user_id, created_at, id)",
    "CREATE INDEX IF NOT EXISTS ix_asset_sections_owner_created "
    "ON asset_sections (tenant_id, created_by_user_id, created_at, id)",
    "CREATE INDEX IF NOT EXISTS ix_asset_groups_owner_created "
    "ON asset_groups (tenant_id, created_by_user_id, created_at, id)",
    "CREATE INDEX IF NOT EXISTS ix_asset_records_owner_created "
    "ON asset_records (tenant_id, created_by_user_id, created_at, id)",
    "CREATE INDEX IF NOT EXISTS ix_knowledge_session_groups_owner_created "
    "ON knowledge_session_groups "
    "(tenant_id, created_by_user_id, workspace_id, created_at, id)",
    "CREATE INDEX IF NOT EXISTS ix_knowledge_sessions_owner_updated "
    "ON knowledge_sessions "
    "(tenant_id, created_by_user_id, workspace_id, updated_at, id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_session_groups_owner_created "
    "ON agent_session_groups "
    "(tenant_id, created_by_user_id, workspace_id, created_at, id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_sessions_owner_updated "
    "ON agent_sessions "
    "(tenant_id, created_by_user_id, workspace_id, updated_at, id)",
    "CREATE INDEX IF NOT EXISTS ix_vector_index_records_owner_created "
    "ON vector_index_records (tenant_id, created_by_user_id, created_at, id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_memory_candidates_owner_status "
    "ON agent_memory_candidates (tenant_id, user_id, status, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_agent_feedback_owner_created "
    "ON agent_feedback (tenant_id, user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_agent_feedback_owner_run "
    "ON agent_feedback (tenant_id, user_id, run_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_quota_usage_subject "
    "ON quota_usage_counters (tenant_id, subject_user_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_quota_limits_user "
    "ON quota_limits (tenant_id, subject_user_id, dimension) "
    "WHERE subject_user_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_quota_limits_default "
    "ON quota_limits (tenant_id, dimension) WHERE subject_user_id IS NULL",
)


_FOREIGN_KEYS = tuple(
    dict.fromkeys(
        (
            *[(spec.table, spec.canonical) for spec in _AUTHORITY_COLUMNS],
            *[(spec.table, spec.canonical) for spec in _SHARE_AUTHORITY_COLUMNS],
            ("auth_sessions", "user_id"),
            ("personal_access_tokens", "owner_user_id"),
            ("local_credentials", "user_id"),
        )
    )
)


_LOCAL_CREDENTIAL_SUBJECT_UNIQUE_SQL = """
DO $migration$
DECLARE
    subject_attnum smallint;
BEGIN
    SELECT attnum INTO subject_attnum
    FROM pg_attribute
    WHERE attrelid = 'local_credentials'::regclass
      AND attname = 'subject'
      AND NOT attisdropped;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'local_credentials'::regclass
          AND contype = 'u'
          AND conkey = ARRAY[subject_attnum]::smallint[]
    ) THEN
        ALTER TABLE local_credentials
            ADD CONSTRAINT uq_local_credentials_subject UNIQUE (subject);
    END IF;
END
$migration$;
"""


_QUOTA_UNIQUENESS_PREFLIGHT_SQL = """
DO $migration$
DECLARE
    duplicate_user_limits bigint;
    duplicate_default_limits bigint;
BEGIN
    SELECT count(*) INTO duplicate_user_limits
    FROM (
        SELECT tenant_id, subject_user_id, dimension
        FROM quota_limits
        WHERE subject_user_id IS NOT NULL
        GROUP BY tenant_id, subject_user_id, dimension
        HAVING count(*) > 1
    ) AS duplicates;
    SELECT count(*) INTO duplicate_default_limits
    FROM (
        SELECT tenant_id, dimension
        FROM quota_limits
        WHERE subject_user_id IS NULL
        GROUP BY tenant_id, dimension
        HAVING count(*) > 1
    ) AS duplicates;
    IF duplicate_user_limits > 0 OR duplicate_default_limits > 0 THEN
        RAISE EXCEPTION
            '0045 found duplicate quota limits: user=%, default=%',
            duplicate_user_limits, duplicate_default_limits;
    END IF;
END
$migration$;
"""


def _final_schema_verify_sql() -> str:
    """Build the fail-closed assertion for the completed hard cut."""
    legacy_columns = tuple(
        dict.fromkeys(
            (
                *[(spec.table, spec.legacy) for spec in _AUTHORITY_COLUMNS],
                ("auth_sessions", "sub"),
                ("personal_access_tokens", "owner_issuer"),
                ("personal_access_tokens", "owner_sub"),
                ("resource_shares", "subject_type"),
                ("resource_shares", "subject_id"),
                ("resource_shares", "granted_by_sub"),
                ("resource_shares", "revoked_by_sub"),
            )
        )
    )
    legacy_values = ", ".join(
        f"('{table}', '{column}')" for table, column in legacy_columns
    )
    canonical_values = ", ".join(
        f"('{table}', '{column}')" for table, column in _FOREIGN_KEYS
    )
    return f"""
DO $migration$
DECLARE
    lingering_legacy text;
    invalid_canonical text;
    lock_columns bigint;
    share_revision_type text;
BEGIN
    SELECT string_agg(legacy.table_name || '.' || legacy.column_name, ', ')
    INTO lingering_legacy
    FROM (VALUES {legacy_values}) AS legacy(table_name, column_name)
    JOIN information_schema.columns AS c
      ON c.table_schema = current_schema()
     AND c.table_name = legacy.table_name
     AND c.column_name = legacy.column_name;
    IF lingering_legacy IS NOT NULL THEN
        RAISE EXCEPTION
            '0045 left legacy authority columns: %', lingering_legacy;
    END IF;

    SELECT string_agg(canonical.table_name || '.' || canonical.column_name, ', ')
    INTO invalid_canonical
    FROM (VALUES {canonical_values}) AS canonical(table_name, column_name)
    LEFT JOIN information_schema.columns AS c
      ON c.table_schema = current_schema()
     AND c.table_name = canonical.table_name
     AND c.column_name = canonical.column_name
     AND c.udt_name = 'uuid'
    WHERE c.column_name IS NULL;
    IF invalid_canonical IS NOT NULL THEN
        RAISE EXCEPTION
            '0045 missing UUID authority columns: %', invalid_canonical;
    END IF;

    IF to_regclass(format('%I.%I', current_schema(), 'groups')) IS NOT NULL
       OR to_regclass(
           format('%I.%I', current_schema(), 'group_members')
       ) IS NOT NULL THEN
        RAISE EXCEPTION '0045 left local group tables behind';
    END IF;

    SELECT count(*) INTO lock_columns
    FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'tenant_security_state';
    IF lock_columns <> 1 THEN
        RAISE EXCEPTION
            '0045 tenant_security_state must remain a pure one-column lock row';
    END IF;

    SELECT udt_name INTO share_revision_type
    FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'resource_shares'
      AND column_name = 'revision';
    IF share_revision_type IS DISTINCT FROM 'int8' THEN
        RAISE EXCEPTION
            '0045 resource_shares.revision must be bigint';
    END IF;
END
$migration$;
"""


def upgrade() -> None:
    """Perform the transactionally locked, irreversible identity hard cut."""
    # Check table presence before constructing the lock statement, then repeat
    # identity uniqueness under the locks so no readiness race is possible.
    op.execute(_required_schema_preflight_sql())
    op.execute(f"LOCK TABLE {_LOCK_TABLES} IN ACCESS EXCLUSIVE MODE")
    op.execute(_required_schema_preflight_sql())
    op.execute(_WORK_AND_SHARE_PREFLIGHT_SQL)

    op.execute(
        """
        CREATE TEMPORARY TABLE _inqtrix_user_subject_map ON COMMIT DROP AS
        SELECT
            tenant_id,
            subject,
            count(*) AS match_count,
            CASE WHEN count(*) = 1
                 THEN min(id::text)::uuid
                 ELSE NULL::uuid
            END AS user_id
        FROM users
        GROUP BY tenant_id, subject
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ON _inqtrix_user_subject_map (tenant_id, subject)"
    )

    for spec in (*_AUTHORITY_COLUMNS, *_SHARE_AUTHORITY_COLUMNS):
        op.execute(_bare_authority_preflight_sql(spec))
    op.execute(_AUTH_SESSION_PREFLIGHT_SQL)
    op.execute(_PAT_PREFLIGHT_SQL)
    op.execute(_CREDENTIAL_PREFLIGHT_SQL)
    op.execute(_QUOTA_DEFAULT_PREFLIGHT_SQL)

    for spec in (*_AUTHORITY_COLUMNS, *_SHARE_AUTHORITY_COLUMNS):
        for statement in _prepare_bare_authority_sql(spec):
            op.execute(statement)
    for statement in _PREPARE_AUTH_SESSION_SQL:
        op.execute(statement)
    for statement in _PREPARE_PAT_SQL:
        op.execute(statement)
    for statement in _PREPARE_CREDENTIAL_SQL:
        op.execute(statement)

    for statement in _USER_CONSTRAINT_SQL:
        op.execute(statement)
    for statement in _TENANT_SECURITY_STATE_SQL:
        op.execute(statement)

    # Unsupported active shares already aborted under ACCESS EXCLUSIVE. Old
    # inactive SubjectRef history has no v0.2 representation and is removed;
    # supported active rows whose resource vanished are retained as explicit
    # soft-revocation history.
    op.execute(_REVOKE_ORPHAN_SHARES_SQL)
    op.execute(_DELETE_UNSUPPORTED_SHARE_HISTORY_SQL)

    for statement in _PRIMARY_KEY_MIGRATIONS:
        op.execute(statement)
    for statement in _QUOTA_PRIMARY_KEY_SQL:
        op.execute(statement)

    for spec in _AUTHORITY_COLUMNS:
        op.execute(_drop_legacy_authority_sql(spec))
    op.execute("ALTER TABLE auth_sessions DROP COLUMN IF EXISTS sub")
    op.execute(
        "ALTER TABLE personal_access_tokens "
        "DROP COLUMN IF EXISTS owner_issuer"
    )
    op.execute(
        "ALTER TABLE personal_access_tokens DROP COLUMN IF EXISTS owner_sub"
    )
    op.execute(_LOCAL_CREDENTIAL_SUBJECT_UNIQUE_SQL)

    for statement in _FINALIZE_SHARES_SQL:
        op.execute(statement)
    op.execute("DROP TABLE group_members")
    op.execute("DROP TABLE groups")

    op.execute(_QUOTA_UNIQUENESS_PREFLIGHT_SQL)
    for statement in _INDEXES:
        op.execute(statement)
    for table, column in _FOREIGN_KEYS:
        op.execute(_foreign_key_sql(table, column))

    op.execute(_final_schema_verify_sql())


def downgrade() -> None:
    """Reject downgrade because issuer information was absent on legacy rows."""
    raise RuntimeError(
        "0045_canonical_user_ids is irreversible: legacy bare subjects did "
        "not retain an issuer, and removed group/share history cannot be "
        "reconstructed safely"
    )

"""Schema bootstrap.

The project deliberately ships no migration tool: the schema is created from the
ORM metadata and then hardened with raw DDL (the append-only trigger, functional
indexes). That keeps the free-tier deployment story to a single command while
still putting the integrity rules *in the database*.

`init_schema` is idempotent and safe to call on every boot. Three properties
matter once the database is a *hosted* one rather than a container on localhost:

* **It must be cheap when there is nothing to do.** `create_all` plus the DDL
  loop is ~40 statements, and every statement is a network round trip. Against a
  database one ocean away that is tens of seconds of start-up on every boot, and
  a platform start-up probe will kill the container long before it finishes. So
  a single-round-trip check runs first and skips the whole thing when the schema
  is already current.

* **It must not open a hole while it runs.** The hardening DDL recreates the
  append-only trigger with `DROP` then `CREATE`. Between those two statements the
  audit log is writable. Skipping the DDL entirely when the guard is already
  installed means the normal boot never opens that window at all.

* **Concurrent boots must not race.** Two replicas starting together could
  interleave their `DROP`/`CREATE`. The apply path takes a transaction-scoped
  advisory lock, so they serialise and the loser sees a current schema.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import CheckConstraint, UniqueConstraint, bindparam, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.logging import get_logger
from app.models.tables import DDL_STATEMENTS, Base

logger = get_logger(__name__)

# Objects created by the raw DDL rather than by the ORM metadata.
_GUARD_TRIGGER = "trg_audit_log_no_update"
_GUARD_FUNCTION = "ledgerlens_audit_log_is_append_only"
_DDL_INDEXES = frozenset({"ix_extractions_vendor_lower"})

# Arbitrary but fixed: the advisory-lock key that serialises schema application
# across processes. Any constant works as long as every replica uses the same one.
_SCHEMA_LOCK_KEY = 0x1EDBE12E

_EXPECTED_TABLES = frozenset(Base.metadata.tables)
_EXPECTED_INDEXES = (
    frozenset(
        index.name
        for table in Base.metadata.tables.values()
        for index in table.indexes
        if index.name is not None
    )
    | _DDL_INDEXES
)

# Named CHECK and UNIQUE constraints. `table.indexes` above does not include
# these, so without them "the schema is current" was a claim about tables and
# indexes only — while the integrity rules this project actually relies on are
# constraints: the enum CHECKs, and `UNIQUE(document_id, fingerprint)` that stops
# re-screening duplicating a finding. A database carrying every table and index
# but none of those would have passed the probe, and in production
# (`DB_MANAGE_SCHEMA=false`) nothing would ever have healed it.
#
# `UNIQUE(file_hash)` — the idempotency key — is already covered: it is declared
# `unique=True, index=True`, so SQLAlchemy emits it as a named unique *index* and
# it appears above. The one gap left is `extractions.document_id`, whose
# column-level `unique=True` produces a constraint the metadata leaves unnamed;
# Postgres names it `extractions_document_id_key`, but that name is the server's
# invention rather than something declared here, so it is not asserted.
_EXPECTED_CONSTRAINTS = frozenset(
    constraint.name
    for table in Base.metadata.tables.values()
    for constraint in table.constraints
    if isinstance(constraint, CheckConstraint | UniqueConstraint) and constraint.name is not None
)

# One statement, one round trip: everything needed to decide whether the boot can
# skip the DDL. Counting rather than listing keeps the result tiny.
_SCHEMA_PROBE = (
    text(
        """
        SELECT
          (SELECT count(*) FROM pg_class c
             JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema()
              AND c.relkind = 'r'
              AND c.relname IN :tables)                       AS tables_present,
          (SELECT count(*) FROM pg_class c
             JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema()
              AND c.relkind = 'i'
              AND c.relname IN :indexes)                      AS indexes_present,
          (SELECT count(*) FROM pg_constraint c
             JOIN pg_namespace n ON n.oid = c.connamespace
            WHERE n.nspname = current_schema()
              AND c.conname IN :constraints)                  AS constraints_present,
          (SELECT count(*) FROM pg_trigger
            WHERE tgname = :guard_trigger AND NOT tgisinternal) AS guard_trigger,
          (SELECT count(*) FROM pg_proc p
             JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = current_schema()
              AND p.proname = :guard_function)                AS guard_function
        """
    )
    .bindparams(
        bindparam("tables", expanding=True),
        bindparam("indexes", expanding=True),
        bindparam("constraints", expanding=True),
    )
    .columns()
)


async def _schema_is_current(connection: AsyncConnection) -> bool:
    """True when every table, index and integrity guard is already installed.

    Deliberately conservative: anything missing returns False and the caller runs
    the full idempotent apply. Adding a table or index therefore heals itself on
    the next boot without needing a migration.
    """
    row = (
        await connection.execute(
            _SCHEMA_PROBE,
            {
                "tables": list(_EXPECTED_TABLES),
                "indexes": list(_EXPECTED_INDEXES),
                "constraints": list(_EXPECTED_CONSTRAINTS),
                "guard_trigger": _GUARD_TRIGGER,
                "guard_function": _GUARD_FUNCTION,
            },
        )
    ).one()
    return bool(
        row.tables_present == len(_EXPECTED_TABLES)
        and row.indexes_present == len(_EXPECTED_INDEXES)
        and row.constraints_present == len(_EXPECTED_CONSTRAINTS)
        and row.guard_trigger == 1
        and row.guard_function == 1
    )


async def _apply_schema(connection: AsyncConnection) -> None:
    """Create tables and apply the hardening DDL. Caller holds the advisory lock."""
    await connection.run_sync(Base.metadata.create_all)
    for statement in DDL_STATEMENTS:
        await connection.execute(text(statement))


class SchemaNotManagedError(RuntimeError):
    """The schema is incomplete and this process is not permitted to build it."""


async def init_schema(engine: AsyncEngine, *, manage: bool = True) -> None:
    """Create tables and apply hardening DDL. Idempotent and safe to run on every boot.

    With ``manage=False`` the schema is *verified* and never modified. That is what
    lets production run as a role holding DML rights and nothing else — no
    ownership, and therefore no ability to `ALTER TABLE audit_log DISABLE TRIGGER`,
    which is the one way to get around the append-only guarantee (AUDIT.md §4c,
    Residual 1).

    It refuses to start rather than continuing against a half-built schema. A
    service that boots and then fails on the first insert with a permission error
    is strictly harder to diagnose than one that says, at boot, exactly which
    command to run and as whom.
    """
    async with engine.connect() as connection:
        if await _schema_is_current(connection):
            logger.info("schema_ready", extra={"tables": len(_EXPECTED_TABLES), "applied": False})
            return

    if not manage:
        logger.error("schema_incomplete_and_unmanaged")
        msg = (
            "the schema is missing or incomplete and DB_MANAGE_SCHEMA is false, so this "
            "process will not create it. Run the migration as the owning role first:\n"
            "  DATABASE_URL='<owner url>' python scripts/grant_app_role.py --migrate\n"
            "This is deliberate: the application runs without ownership so it cannot "
            "disable the audit log's append-only trigger."
        )
        raise SchemaNotManagedError(msg)

    async with engine.begin() as connection:
        # Serialise concurrent boots; released automatically when the transaction ends.
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _SCHEMA_LOCK_KEY}
        )
        # Re-check under the lock: a replica that raced us may have just finished.
        if await _schema_is_current(connection):
            logger.info("schema_ready", extra={"tables": len(_EXPECTED_TABLES), "applied": False})
            return
        await _apply_schema(connection)

    logger.info("schema_ready", extra={"tables": len(_EXPECTED_TABLES), "applied": True})


async def wait_for_database(
    engine: AsyncEngine,
    *,
    max_attempts: int = 5,
    initial_delay_s: float = 1.0,
) -> None:
    """Block until the database answers, with bounded exponential backoff.

    A serverless Postgres (Neon, Aurora Serverless) suspends its compute when
    idle and takes several seconds to wake. Without this, the first connection of
    a cold boot can exceed the driver timeout and the process dies with an opaque
    error even though the database is perfectly healthy a second later.

    Raises the last error once the attempts are exhausted, so a genuinely
    unreachable database still fails loudly rather than hanging for ever.
    """
    delay = initial_delay_s
    for attempt in range(1, max_attempts + 1):
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            if attempt > 1:
                logger.info("database_ready", extra={"attempts": attempt})
            return
        except (SQLAlchemyError, OSError) as exc:  # TimeoutError subclasses OSError
            if attempt == max_attempts:
                logger.error(
                    "database_unreachable",
                    extra={"attempts": attempt, "error": type(exc).__name__},
                )
                raise
            logger.warning(
                "database_not_ready_retrying",
                extra={"attempt": attempt, "delay_s": delay, "error": type(exc).__name__},
            )
            await asyncio.sleep(delay)
            delay *= 2


async def drop_schema(engine: AsyncEngine) -> None:
    """Drop every LedgerLens table. Used only by the integration test fixtures."""
    async with engine.begin() as connection:
        # The append-only trigger blocks DELETE but not DROP TABLE; remove it first
        # so a test teardown cannot be tripped up by its own guard rail.
        await connection.execute(text(f"DROP TRIGGER IF EXISTS {_GUARD_TRIGGER} ON audit_log;"))
        await connection.run_sync(Base.metadata.drop_all)
    logger.info("schema_dropped")

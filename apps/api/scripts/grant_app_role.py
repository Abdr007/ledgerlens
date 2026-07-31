"""Split the database credential in two, and prove the split holds.

AUDIT.md §4c, Residual 1: the service connected as the table owner, because it
creates its own schema on boot. Ownership is what permits
`ALTER TABLE audit_log DISABLE TRIGGER` — so the credential the service carries
could switch off the append-only guarantee and then delete history, in two
statements. A trigger stops `DELETE`; it does not stop being turned off.

The convenience and the weakness were the same privilege, so they are separated
here rather than traded away:

* an **owning** role runs the DDL — this script, run by a human, occasionally;
* an **application** role gets `SELECT`/`INSERT`/`UPDATE` and nothing else, which
  is the complete set the request path uses. No `DELETE`, no `TRUNCATE`, no
  ownership, no `ALTER`.

Run with the owner's URL in `DATABASE_URL`:

    DATABASE_URL='<owner url>' python scripts/grant_app_role.py --migrate

It creates the role if missing, applies the schema, grants exactly those rights,
and then **connects as the new role and tries to disable the trigger**, because a
privilege boundary nobody tested is a privilege boundary nobody has. The
resulting connection URL is written to `apps/api/.env.app` (gitignored) rather
than printed, so it does not land in a terminal scrollback or a chat transcript.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import string
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

# Allow `python scripts/grant_app_role.py` from the apps/api directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.bootstrap import init_schema
from app.core.db import dispose_engine, init_engine
from app.core.logging import configure_logging
from app.core.settings import Settings, get_settings

APP_ROLE = "ledgerlens_app"
OUTPUT = Path(__file__).resolve().parent.parent / ".env.app"

#: Exactly what the request path uses. `DELETE` is deliberately absent: nothing in
#: `app/` deletes a row, and withholding it means even a SQL-injection reaching
#: this connection cannot remove a document, let alone an audit record.
GRANTS = (
    f"GRANT CONNECT ON DATABASE {{database}} TO {APP_ROLE}",
    f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}",
    f"GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}",
    f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}",
)

#: Anything created later inherits the same ceiling, so a new table does not
#: silently arrive with no grants and take the service down on its first insert.
DEFAULTS = (
    f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
    f"GRANT SELECT, INSERT, UPDATE ON TABLES TO {APP_ROLE}",
    f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}",
)


def _app_url(owner_url: str, password: str) -> str:
    """The owner's URL with the credential swapped for the application role's."""
    parts = urlsplit(owner_url)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{APP_ROLE}:{quote(password, safe='')}@{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _new_password() -> str:
    """A password that is safe to inline into DDL, by construction.

    `CREATE ROLE` and `ALTER ROLE` are utility statements: PostgreSQL will not
    accept a bind parameter for the password, so it has to be interpolated. Rather
    than escape a value that might contain a quote, the value is drawn from an
    alphabet that cannot — 62 symbols over 40 characters is ~238 bits, so nothing
    is given up for the guarantee.
    """
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(40))


async def _ensure_role(connection: AsyncConnection, password: str) -> bool:
    # Belt and braces: the interpolation below is only safe because of this.
    if not password.isalnum():
        msg = "the generated password must be alphanumeric before it is inlined into DDL"
        raise ValueError(msg)

    exists = await connection.scalar(
        text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": APP_ROLE}
    )
    # Neither the role name nor the password can be bound here. The role name is a
    # module constant and the password is alphanumeric by construction, so there is
    # no caller-supplied text in either statement.
    verb = "ALTER" if exists else "CREATE"
    suffix = "WITH LOGIN PASSWORD" if exists else "LOGIN PASSWORD"
    await connection.execute(text(f"{verb} ROLE {APP_ROLE} {suffix} '{password}'"))
    return not exists


async def run(*, migrate: bool) -> int:
    configure_logging("WARNING")
    settings = get_settings()
    owner_url = settings.database_url
    engine = init_engine(settings)

    password = _new_password()
    try:
        if migrate:
            await init_schema(engine, manage=True)
            print("  schema applied as the owning role")

        async with engine.begin() as connection:
            database = await connection.scalar(text("SELECT current_database()"))
            owner = await connection.scalar(text("SELECT current_user"))
            print(f"  owner   {owner} on {database}")

            created = await _ensure_role(connection, password)
            print(f"  role    {APP_ROLE} {'created' if created else 'password rotated'}")

            for statement in GRANTS:
                await connection.execute(text(statement.format(database=database)))
            for statement in DEFAULTS:
                await connection.execute(text(statement))
            print(f"  granted SELECT, INSERT, UPDATE — and nothing else — to {APP_ROLE}")
    finally:
        await dispose_engine()

    app_url = _app_url(owner_url, password)
    verdict = await _prove_the_boundary(app_url)
    OUTPUT.write_text(
        "# Least-privilege application credential, written by scripts/grant_app_role.py.\n"
        "# Gitignored. Set this as DATABASE_URL on the Space, with DB_MANAGE_SCHEMA=false.\n"
        f"DATABASE_URL={app_url}\n",
        encoding="utf-8",
    )
    print(f"  wrote   {OUTPUT.relative_to(OUTPUT.parents[2])} (gitignored, not printed)")
    return verdict


def _why(exc: BaseException) -> str:
    """The database's own reason, not SQLAlchemy's wrapper around it.

    `str()` on a wrapped driver error leads with the exception class and the
    dialect path, so the first 60 characters are all scaffolding and the actual
    words `permission denied` never appear. This check exists to be read, and a
    refusal nobody can read is indistinguishable from a refusal that did not happen.
    """
    original = getattr(exc, "orig", None) or exc
    text_form = str(original).strip()
    return text_form.splitlines()[0][:80] if text_form else type(original).__name__


async def _prove_the_boundary(app_url: str) -> int:
    """Connect as the application role and try to defeat the guarantee.

    Three attempts, each of which the old credential could do and the new one must
    not: turn the trigger off, delete an audit record, and delete a document (whose
    cascade reaches the audit log).
    """
    settings = Settings(database_url=app_url, db_manage_schema=False)
    # `dispose_engine` above cleared the module-level engine, so this builds a
    # fresh one bound to the least-privilege credential.
    engine = init_engine(settings)
    attempts = (
        ("disable the append-only trigger", "ALTER TABLE audit_log DISABLE TRIGGER ALL"),
        (
            "delete an audit record",
            "DELETE FROM audit_log WHERE id = (SELECT min(id) FROM audit_log)",
        ),
        (
            "delete a document",
            "DELETE FROM documents WHERE id = (SELECT id FROM documents LIMIT 1)",
        ),
    )
    print(f"\n  proving the boundary as {APP_ROLE}:")
    failures = 0
    try:
        for label, statement in attempts:
            try:
                async with engine.begin() as connection:
                    await connection.execute(text(statement))
                print(f"    FAIL  {label:32} PERMITTED — the boundary does not hold")
                failures += 1
            except Exception as exc:
                print(f"    PASS  {label:32} refused: {_why(exc)}")

        # It must still be able to do its actual job.
        async with engine.connect() as connection:
            documents = await connection.scalar(text("SELECT count(*) FROM documents"))
        print(f"    PASS  {'can still read the ledger':32} {documents} documents")
    finally:
        await dispose_engine()

    if failures:
        print(f"\n  {failures} of {len(attempts)} boundaries did not hold\n")
        return 1
    print("\n  the application credential can read and write, and cannot rewrite history\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="apply the schema as the owner before granting (needed on a fresh database)",
    )
    args = parser.parse_args(argv)
    return asyncio.run(run(migrate=args.migrate))


if __name__ == "__main__":
    sys.exit(main())

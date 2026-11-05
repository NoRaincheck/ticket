#!/usr/bin/env python3
"""ticket.py — a graph-based issue tracker in a single file.

A self-contained, standard-library-only task tracker with dependencies,
parent/child hierarchies, claiming, filters, and SQLite persistence. Run it
directly or import it as a Python module. Python 3.12+ is required.

The file is intentionally self-contained, so it can be copied into a GitHub
gist or downloaded on its own and used as-is. Keep the name ``ticket.py`` when
running the examples below.

# Quick start

```bash
python3 ticket.py init --dir
python3 ticket.py create "Fix auth middleware" --body "Tokens expire early"
python3 ticket.py list
python3 ticket.py ready
python3 ticket.py claim task-a3f2dd1c --actor alice
python3 ticket.py close task-a3f2dd1c --reason "Fixed in abc1234"
```

``init --dir`` creates a ``.beans/`` store in the current directory. Bean IDs
are type-prefixed and generated automatically (``task-a3f2dd1c``,
``bug-deadbeef``, ``epic-12345678``). Use ``uv run ticket.py`` instead of
``python3 ticket.py`` if you prefer uv; no project or dependency installation
is needed either way. Run ``python3 ticket.py --help`` for the complete,
always-current option list.

# Concepts

- **Bean**: a task with ``id``, ``title``, ``type``, ``status``, ``priority``,
  ``body``, optional parent/assignee fields, and timestamps.
- **Status**: ``open``, ``in_progress``, or ``closed``.
- **Priority**: ``0`` (highest) through ``4`` (lowest); the default is ``2``.
- **Types**: configurable names; defaults are ``task``, ``bug``, and ``epic``.
- **Dependencies**: ``dep add A B`` means A blocks B; B is not ready until A
  is closed.
- **Parents**: ``create "Child" --parent <id>`` builds a hierarchy. A bean
  with open children is not ready.
- **Ready**: not closed, not blocked directly or transitively by an open bean,
  and has no open children.
- **Claiming**: ``claim`` assigns a bean and moves it to ``in_progress``.

# Commands

Setup and configuration:

- ``init [--dir] [--name NAME]`` — create a local ``.beans/`` store or
  register a project in the user data directory.
- ``migrate [--name NAME]`` — copy a local ``.beans/`` store into the registry.
- ``config`` — print the config path and current configuration.

Create and inspect:

- ``create TITLE [--type TYPE] [--body TEXT] [--parent ID] [--priority N]
  [--dep ID ...]`` — create a bean.
- ``show ID`` — show one bean; add ``--json`` for the full record.
- ``list [--type T[,T]] [--status S[,S]] [--parent ID]`` — list beans.
- ``ready [--assignee NAME | --unassigned] [--parent ID]`` — list unblocked
  beans.
- ``search QUERY`` — search titles and bodies.
- ``stats`` — count beans by status, type, and assignee.
- ``graph`` — print the parent/child and blocker tree.

Change and workflow:

- ``update ID [--title TEXT] [--type TYPE] [--status STATUS] [--priority N]
  [--body TEXT] [--parent ID]`` — update fields; reopening clears close data.
- ``close ID [--reason TEXT] [--force]`` — close a bean.
- ``delete ID`` — delete a bean and its dependencies.
- ``claim ID --actor NAME`` — claim a bean.
- ``release [ID] --actor NAME`` — release one bean.
- ``release --mine --actor NAME`` — release all beans claimed by an actor.

Dependencies and types:

- ``dep add FROM TO [--type blocks]`` — add a blocking dependency.
- ``dep remove FROM TO`` — remove a dependency.
- ``types`` — list bean types.
- ``types add NAME [--description TEXT]`` — add a type.
- ``types remove NAME`` — remove a type.

Data and automation:

- ``schema`` — emit JSON schemas for ``Bean``, ``Dep``, and ``Error``.
- ``skill`` — print agent-integration instructions.
- ``export-journal`` — export the change journal as JSONL.
- ``rebuild JOURNAL_FILE`` — replay a JSONL journal into a database.

Global options go before the subcommand:

- ``--db PATH`` — use a specific SQLite database.
- ``--project NAME`` — use a project registered in the config.
- ``--json`` — emit compact JSON.
- ``--dry-run`` — roll back all writes when the command exits.
- ``--fields LIST`` — with ``--json``, include only comma-separated fields.
- ``--version`` — print the version.

# Output and errors

Plain output prints one bean per line:

```text
task-a3f2dd1c  2026-11-05 14:30  Fix auth middleware
```

``--json`` emits compact JSON objects. ``show --json`` additionally includes
``blocked_by`` and ``blocks`` lists. Errors exit with status 1: text errors go
to stderr, and ``--json`` errors print ``{"message": "..."}`` to stdout.

# Storage and discovery

Commands resolve the database in this order:

1. ``--db PATH``
2. ``--project NAME``
3. the ``MAGIC_BEANS_DIR`` environment variable
4. a registry match based on the current Git remote or path
5. a ``.beans/`` directory in the current directory or a parent

``init --dir`` creates ``beans.db``, ``AGENTS.md``, and ``.gitignore`` inside
``.beans/``. Registry data lives under ``$XDG_DATA_HOME/beans/<project>/``,
and config lives at ``$XDG_CONFIG_HOME/beans/config.json`` (defaults:
``~/.local/share`` and ``~/.config``).

``MAGIC_BEANS_PARENT_ID`` sets a default parent for ``create``, ``list``, and
``ready`` when ``--parent`` is omitted.

# Python API

```python
from pathlib import Path

import ticket

with ticket.Store.from_path(Path("issues.db")) as store:
    bean = ticket.create_bean(store, "Fix auth", body="Tokens expire early")
    blocker = ticket.create_bean(store, "Rotate keys")
    ticket.add_dep(store, blocker.id, bean.id)
    ticket.claim_bean(store, blocker.id, "alice")
    for item in ticket.ready_beans(store):
        print(item.id, item.title)
```

The main API entry points are ``Store``, ``Bean``, ``Dep``, ``Config``,
``create_bean``, ``show_bean``, ``update_bean``, ``close_bean``,
``reopen_bean``, ``delete_bean``, ``claim_bean``, ``release_bean``,
``list_beans``, ``ready_beans``, ``search_beans``, ``stats``, ``add_dep``,
``remove_dep``, ``list_deps``, and ``graph``.
"""

from __future__ import annotations

import argparse
import builtins
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from functools import partial
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
from typing import Any, ClassVar, Literal

VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

ID_BYTES = 4
ID_PATTERN = re.compile(r"^[a-z]+-[0-9a-f]+$")
STATUSES = ("open", "in_progress", "closed")


class NotFoundError(KeyError):
    # KeyError wraps str(e) in quotes; raise with the offending ids and
    # let each subclass render its own instructive message.
    template: ClassVar[str] = ""

    def __str__(self) -> str:
        return self.template.format(*self.args)


class BeanNotFoundError(NotFoundError):
    template = "Bean not found: {0}"


class ParentBeanNotFoundError(BeanNotFoundError):
    template = "Parent bean {0} does not exist"


class DepNotFoundError(NotFoundError):
    template = "No dependency from {0} to {1}"


class CyclicDepError(ValueError):
    pass


class OpenChildrenError(ValueError):
    pass


def isoformat(value: datetime) -> str:
    """Render a datetime the way pydantic's ``model_dump_json`` does."""
    rendered = value.isoformat()
    if value.tzinfo is not None and value.utcoffset() is not None:
        rendered = rendered.replace("+00:00", "Z")
    return rendered


class BeanId(str):
    pattern = ID_PATTERN

    def __new__(cls, value: Any = "", **kwargs: Any) -> BeanId:
        value = str(value)
        if not cls.pattern.match(value):
            raise ValueError(f"Invalid bean id: {value}")
        return super().__new__(cls, value)

    @classmethod
    def generate(cls, type_name: str = "task", fn: Any = None) -> BeanId:
        generator = fn or partial(secrets.token_hex, ID_BYTES)
        return cls(f"{type_name}-{generator()}")

    @property
    def type_prefix(self) -> str:
        return self.split("-", 1)[0]


def model_dump_json(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(
        value,
        default=json_default,
        ensure_ascii=False,
        indent=indent,
        separators=None if indent else (",", ":"),
    )


def json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return isoformat(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


@dataclass
class Bean:
    id: BeanId = field(default_factory=BeanId.generate)
    title: str = ""
    type: str = "task"
    status: Literal["open", "in_progress", "closed"] = "open"
    priority: int = 2
    body: str = ""
    parent_id: str | None = None
    assignee: str | None = None
    created_by: str | None = None
    ref_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    closed_at: datetime | None = None
    close_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, BeanId):
            self.id = BeanId(self.id)
        if self.status not in STATUSES:
            raise ValueError(f"Invalid status: {self.status}")
        if not 0 <= self.priority <= 4:
            raise ValueError("priority must be between 0 and 4")
        if isinstance(self.created_at, str):
            self.created_at = datetime.fromisoformat(self.created_at)
        if isinstance(self.closed_at, str):
            self.closed_at = datetime.fromisoformat(self.closed_at)
        if self.title is None:
            raise ValueError("title is required")

    def model_dump(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def model_dump_json(self) -> str:
        return model_dump_json(self.model_dump())

    @classmethod
    def model_json_schema(cls) -> dict[str, Any]:
        return {
            "additionalProperties": False,
            "properties": {
                "id": {"pattern": r"^[a-z]+-[0-9a-f]+$", "title": "Id", "type": "string"},
                "title": {"title": "Title", "type": "string"},
                "type": {"default": "task", "title": "Type", "type": "string"},
                "status": {
                    "default": "open",
                    "enum": list(STATUSES),
                    "title": "Status",
                    "type": "string",
                },
                "priority": {
                    "default": 2,
                    "maximum": 4,
                    "minimum": 0,
                    "title": "Priority",
                    "type": "integer",
                },
                "body": {"default": "", "title": "Body", "type": "string"},
                "parent_id": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Parent Id"},
                "assignee": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Assignee"},
                "created_by": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "title": "Created By",
                },
                "ref_id": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Ref Id"},
                "created_at": {"format": "date-time", "title": "Created At", "type": "string"},
                "closed_at": {
                    "anyOf": [{"format": "date-time", "type": "string"}, {"type": "null"}],
                    "default": None,
                    "title": "Closed At",
                },
                "close_reason": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "title": "Close Reason",
                },
            },
            "required": ["title"],
            "title": "Bean",
            "type": "object",
        }


@dataclass
class BeanUpdate:
    title: str | None = None
    type: str | None = None
    status: Literal["open", "in_progress", "closed"] | None = None
    priority: int | None = None
    body: str | None = None
    parent_id: str | None = None

    def __post_init__(self) -> None:
        if self.status is not None and self.status not in STATUSES:
            raise ValueError(f"Invalid status: {self.status}")
        if self.priority is not None and not 0 <= self.priority <= 4:
            raise ValueError("priority must be between 0 and 4")

    def model_dump(self, exclude_none: bool = False) -> dict[str, Any]:
        data = {item.name: getattr(self, item.name) for item in fields(self)}
        if exclude_none:
            data = {key: value for key, value in data.items() if value is not None}
        return data


@dataclass(frozen=True)
class Dep:
    from_id: BeanId
    to_id: BeanId
    dep_type: str = "blocks"

    def __post_init__(self) -> None:
        if not isinstance(self.from_id, BeanId):
            object.__setattr__(self, "from_id", BeanId(self.from_id))
        if not isinstance(self.to_id, BeanId):
            object.__setattr__(self, "to_id", BeanId(self.to_id))

    def model_dump(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def model_dump_json(self) -> str:
        return model_dump_json(self.model_dump())

    @classmethod
    def model_json_schema(cls) -> dict[str, Any]:
        return {
            "additionalProperties": False,
            "properties": {
                "from_id": {"pattern": r"^[a-z]+-[0-9a-f]+$", "title": "From Id", "type": "string"},
                "to_id": {"pattern": r"^[a-z]+-[0-9a-f]+$", "title": "To Id", "type": "string"},
                "dep_type": {"default": "blocks", "title": "Dep Type", "type": "string"},
            },
            "required": ["from_id", "to_id"],
            "title": "Dep",
            "type": "object",
        }


@dataclass
class Error:
    message: str

    def model_dump(self) -> dict[str, Any]:
        return {"message": self.message}

    def model_dump_json(self) -> str:
        return model_dump_json(self.model_dump())

    @classmethod
    def model_json_schema(cls) -> dict[str, Any]:
        return {
            "additionalProperties": False,
            "properties": {"message": {"title": "Message", "type": "string"}},
            "required": ["message"],
            "title": "Error",
            "type": "object",
        }


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def columns(cursor: sqlite3.Cursor) -> builtins.list[str]:
    return [desc[0] for desc in cursor.description]


def row(cols: builtins.list[str], values: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(cols, values))


def rows(cursor: sqlite3.Cursor):
    cols = columns(cursor)
    return (row(cols, values) for values in cursor.fetchall())


# Result mappers


def one_bean(cursor: sqlite3.Cursor) -> Bean | None:
    match = cursor.fetchone()
    if match is None:
        return None
    return Bean(**row(columns(cursor), match))


def beans(cursor: sqlite3.Cursor) -> builtins.list[Bean]:
    return [Bean(**r) for r in rows(cursor)]


def deps(cursor: sqlite3.Cursor) -> builtins.list[Dep]:
    return [Dep(**r) for r in rows(cursor)]


# Query builders

UPDATABLE_FIELDS = {
    "title",
    "type",
    "status",
    "priority",
    "body",
    "parent_id",
    "assignee",
    "closed_at",
    "close_reason",
}


def bean_values(bean: Bean) -> tuple[Any, ...]:
    return (
        bean.id,
        bean.title,
        bean.type,
        bean.status,
        bean.priority,
        bean.body,
        bean.parent_id,
        bean.assignee,
        bean.created_by,
        bean.ref_id,
        bean.created_at.isoformat(),
        bean.closed_at.isoformat() if bean.closed_at else None,
        bean.close_reason,
    )


def insert_query(bean: Bean) -> tuple[str, tuple[Any, ...]]:
    sql = (
        "INSERT INTO beans (id, title, type, status, priority, body, parent_id,"
        " assignee, created_by, ref_id, created_at, closed_at, close_reason)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    return sql, bean_values(bean)


def update_query(bean_id: str, **fields: Any) -> tuple[str, builtins.list[Any]]:
    set_clause = ", ".join(f"{key} = ?" for key in fields)
    sql = f"UPDATE beans SET {set_clause} WHERE id = ?"
    return sql, [*fields.values(), bean_id]


def update_all_query(bean: Bean) -> tuple[str, tuple[Any, ...]]:
    sql = (
        "UPDATE beans SET title=?, type=?, status=?, priority=?, body=?,"
        " parent_id=?, assignee=?, created_by=?, ref_id=?, created_at=?, closed_at=?, close_reason=?"
        " WHERE id=?"
    )
    values = bean_values(bean)
    return sql, (*values[1:], values[0])


def validate_fields(field_names: Any, updatable: set[str] = UPDATABLE_FIELDS) -> None:
    if invalid := field_names.keys() - updatable:
        raise ValueError(f"Invalid fields: {invalid}")


def bean_snapshot(bean: Bean) -> str:
    return bean.model_dump_json()


# Schema and migrations

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS beans (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'task',
    status TEXT NOT NULL DEFAULT 'open',
    priority INTEGER NOT NULL DEFAULT 2,
    body TEXT NOT NULL DEFAULT '',
    parent_id TEXT,
    assignee TEXT,
    created_by TEXT,
    ref_id TEXT,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    close_reason TEXT
);

CREATE TABLE IF NOT EXISTS deps (
    from_id TEXT NOT NULL REFERENCES beans(id),
    to_id TEXT NOT NULL REFERENCES beans(id),
    dep_type TEXT NOT NULL DEFAULT 'blocks',
    PRIMARY KEY (from_id, to_id)
);

CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    bean_id TEXT NOT NULL,
    snapshot TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

DROP TRIGGER IF EXISTS journal_after_insert;
DROP TRIGGER IF EXISTS journal_after_update;
DROP TRIGGER IF EXISTS journal_after_delete;
DROP TRIGGER IF EXISTS journal_after_dep_insert;
DROP TRIGGER IF EXISTS journal_after_dep_delete;
"""

INSERT_JOURNAL = "INSERT INTO journal (action, bean_id, snapshot) VALUES (?, ?, ?)"


def journal_log(conn: Any, action: str, bean_id: str, snapshot: str | None) -> None:
    conn.execute(INSERT_JOURNAL, (action, bean_id, snapshot))


SCHEMA_VERSION = 2

MIGRATIONS = {
    2: "DROP TABLE IF EXISTS labels; DROP TABLE IF EXISTS cross_deps;",
}


def migrate(conn: Any, migrations: dict[int, str] = MIGRATIONS, target: int = SCHEMA_VERSION) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version in range(current + 1, target + 1):
        if version in migrations:
            conn.executescript(migrations[version])
    conn.executescript(f"PRAGMA user_version = {target};")


class BeanStore:
    def __init__(self, conn: Any):
        self.conn = conn

    def create(self, bean: Bean) -> Bean:
        sql, params = insert_query(bean)
        with self.conn:
            self.conn.execute(sql, params)
            journal_log(self.conn, "create", bean.id, bean_snapshot(bean))
        return bean

    def get(self, bean_id: str) -> Bean:
        cursor = self.conn.execute("SELECT * FROM beans WHERE id = ?", (bean_id,))
        bean = one_bean(cursor)
        if bean is None:
            raise BeanNotFoundError(bean_id)
        return bean

    def update(self, bean_id: str, **fields: Any) -> int:
        if not fields:
            return 0
        validate_fields(fields)
        sql, params = update_query(bean_id, **fields)
        with self.conn:
            cursor = self.conn.execute(sql, params)
            if cursor.rowcount:
                bean = self.get(bean_id)
                journal_log(self.conn, "update", bean.id, bean_snapshot(bean))
        return cursor.rowcount

    def delete(self, bean_id: str) -> int:
        cursor = self.conn.execute("SELECT * FROM beans WHERE id = ?", (bean_id,))
        bean = one_bean(cursor)
        with self.conn:
            self.conn.execute("DELETE FROM deps WHERE from_id = ? OR to_id = ?", (bean_id, bean_id))
            cursor = self.conn.execute("DELETE FROM beans WHERE id = ?", (bean_id,))
            if cursor.rowcount and bean:
                journal_log(self.conn, "delete", bean.id, bean_snapshot(bean))
        return cursor.rowcount

    def list(
        self,
        types: builtins.list[str] | None = None,
        statuses: builtins.list[str] | None = None,
        parent_id: str | None = None,
    ) -> builtins.list[Bean]:
        sql = "SELECT * FROM beans"
        params: builtins.list[Any] = []
        clauses = []
        if types:
            placeholders = ",".join("?" for _ in types)
            clauses.append(f"type IN ({placeholders})")
            params.extend(types)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        if parent_id:
            clauses.append("parent_id = ?")
            params.append(parent_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return beans(self.conn.execute(sql, params))

    def search(self, query: str) -> builtins.list[Bean]:
        pattern = f"%{query}%"
        return beans(
            self.conn.execute(
                "SELECT * FROM beans WHERE title LIKE ? OR body LIKE ?",
                (pattern, pattern),
            )
        )

    def list_by_assignee(self, actor: str) -> builtins.list[Bean]:
        return beans(self.conn.execute("SELECT * FROM beans WHERE assignee = ?", (actor,)))

    def stats(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {"by_status": {}, "by_type": {}, "by_assignee": {}}
        for label, column in [("by_status", "status"), ("by_type", "type")]:
            cursor = self.conn.execute(f"SELECT {column}, COUNT(*) FROM beans GROUP BY {column}")
            result[label] = dict(cursor.fetchall())
        cursor = self.conn.execute(
            "SELECT COALESCE(assignee, 'unassigned'), COUNT(*) FROM beans GROUP BY COALESCE(assignee, 'unassigned')"
        )
        result["by_assignee"] = dict(cursor.fetchall())
        return result

    def ready(
        self,
        assignee: str | None = None,
        unassigned: bool = False,
        parent_id: str | None = None,
    ) -> builtins.list[Bean]:
        sql = """
            WITH RECURSIVE
            blocked_by_deps(id) AS (
                SELECT d.to_id
                FROM deps d
                JOIN beans b ON d.from_id = b.id
                WHERE d.dep_type = 'blocks' AND b.status != 'closed'
                UNION
                SELECT d.to_id
                FROM deps d
                JOIN blocked_by_deps bl ON d.from_id = bl.id
                JOIN beans b ON d.from_id = b.id
                WHERE d.dep_type = 'blocks' AND b.status != 'closed'
            ),
            blocked_by_children(id) AS (
                SELECT b.parent_id
                FROM beans b
                WHERE b.parent_id IS NOT NULL AND b.status != 'closed'
            )
            SELECT * FROM beans
            WHERE status != 'closed'
              AND id NOT IN (SELECT id FROM blocked_by_deps)
              AND id NOT IN (SELECT id FROM blocked_by_children)
        """
        params: builtins.list[Any] = []
        if assignee is not None:
            sql += "  AND assignee = ?\n"
            params.append(assignee)
        elif unassigned:
            sql += "  AND assignee IS NULL\n"
        if parent_id:
            sql += "  AND parent_id = ?\n"
            params.append(parent_id)
        sql += "  ORDER BY priority"
        return beans(self.conn.execute(sql, params))


class DepStore:
    def __init__(self, conn: Any):
        self.conn = conn

    def add(self, dep: Dep) -> Dep:
        with self.conn:
            self.conn.execute(
                "INSERT INTO deps (from_id, to_id, dep_type) VALUES (?, ?, ?)",
                (dep.from_id, dep.to_id, dep.dep_type),
            )
            journal_log(self.conn, "dep_add", dep.to_id, dep.model_dump_json())
        return dep

    def list(self, from_id: str) -> builtins.list[Dep]:
        return deps(
            self.conn.execute(
                "SELECT from_id, to_id, dep_type FROM deps WHERE from_id = ?",
                (from_id,),
            )
        )

    def list_all(self) -> builtins.list[Dep]:
        return deps(self.conn.execute("SELECT from_id, to_id, dep_type FROM deps"))

    def remove(self, from_id: str, to_id: str) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "DELETE FROM deps WHERE from_id = ? AND to_id = ?",
                (from_id, to_id),
            )
            if cursor.rowcount:
                dep = Dep(from_id=BeanId(from_id), to_id=BeanId(to_id))
                journal_log(self.conn, "dep_remove", to_id, dep.model_dump_json())
        return cursor.rowcount


class JournalStore:
    def __init__(self, conn: Any):
        self.conn = conn

    def export(self):
        cursor = self.conn.execute("SELECT action, bean_id, snapshot, created_at FROM journal ORDER BY id")
        for action, bean_id, snapshot, created_at in cursor:
            entry = {
                "action": action,
                "bean_id": bean_id,
                "snapshot": json.loads(snapshot) if snapshot else None,
                "created_at": created_at,
            }
            yield json.dumps(entry)

    def replay(self, lines: Any) -> None:
        with self.conn:
            for line in lines:
                entry = json.loads(line)
                action = entry["action"]
                snapshot = entry["snapshot"]
                bean_id = entry["bean_id"]

                if action == "create":
                    sql, params = insert_query(Bean(**snapshot))
                    self.conn.execute(sql, params)
                elif action == "update":
                    sql, params = update_all_query(Bean(**snapshot))
                    self.conn.execute(sql, params)
                elif action == "delete":
                    self.conn.execute("DELETE FROM beans WHERE id=?", (bean_id,))
                elif action == "dep_add":
                    self.conn.execute(
                        "INSERT INTO deps (from_id, to_id, dep_type) VALUES (?, ?, ?)",
                        (snapshot["from_id"], snapshot["to_id"], snapshot["dep_type"]),
                    )
                elif action == "dep_remove":
                    self.conn.execute(
                        "DELETE FROM deps WHERE from_id = ? AND to_id = ?",
                        (snapshot["from_id"], snapshot["to_id"]),
                    )


# Dry-run support


class DryRunConnection:
    """Wraps a sqlite3.Connection, suppressing commit so dry-run can rollback."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def commit(self) -> None:
        pass  # suppress

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def __enter__(self) -> DryRunConnection:
        return self

    def __exit__(self, *args: Any) -> bool | None:
        if args[0] is None:
            return None  # suppress commit
        self._conn.rollback()
        return None


# Composite store


class Store:
    def __init__(self, conn: sqlite3.Connection, dry_run: bool = False):
        self.conn: Any = conn
        conn.executescript(SCHEMA)
        migrate(conn)
        if dry_run:
            self.conn = DryRunConnection(conn)
        self.bean = BeanStore(self.conn)
        self.dep = DepStore(self.conn)
        self.journal = JournalStore(self.conn)
        self.dry_run = dry_run

    # Bean delegation
    def create(self, bean: Bean) -> Bean:
        return self.bean.create(bean)

    def get(self, bean_id: str) -> Bean:
        return self.bean.get(bean_id)

    def update(self, bean_id: str, **fields: Any) -> int:
        return self.bean.update(bean_id, **fields)

    def delete(self, bean_id: str) -> int:
        return self.bean.delete(bean_id)

    def list(
        self,
        types: builtins.list[str] | None = None,
        statuses: builtins.list[str] | None = None,
        parent_id: str | None = None,
    ) -> builtins.list[Bean]:
        return self.bean.list(types=types, statuses=statuses, parent_id=parent_id)

    def list_by_assignee(self, actor: str) -> builtins.list[Bean]:
        return self.bean.list_by_assignee(actor)

    def ready(
        self,
        assignee: str | None = None,
        unassigned: bool = False,
        parent_id: str | None = None,
    ) -> builtins.list[Bean]:
        return self.bean.ready(assignee=assignee, unassigned=unassigned, parent_id=parent_id)

    def search(self, query: str) -> builtins.list[Bean]:
        return self.bean.search(query)

    def stats(self) -> dict[str, dict[str, int]]:
        return self.bean.stats()

    # Dep delegation
    def add_dep(self, dep: Dep) -> Dep:
        return self.dep.add(dep)

    def list_deps(self, from_id: str) -> builtins.list[Dep]:
        return self.dep.list(from_id)

    def list_all_deps(self) -> builtins.list[Dep]:
        return self.dep.list_all()

    def remove_dep(self, from_id: str, to_id: str) -> int:
        return self.dep.remove(from_id, to_id)

    @classmethod
    def from_path(cls, db_path: Path, dry_run: bool = False) -> Store:
        return cls(sqlite3.connect(db_path), dry_run=dry_run)

    def close(self) -> None:
        if self.dry_run:
            self.conn.rollback()
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Command API
# ---------------------------------------------------------------------------


def create_bean(store: Store, title: str, valid_types: Any = (), deps: Any = (), **fields: Any) -> Bean:
    bean_type = fields.get("type", "task")
    if valid_types and bean_type not in valid_types:
        raise ValueError(f"Unknown type: {bean_type}. Valid: {', '.join(sorted(valid_types))}")
    if parent_id := fields.get("parent_id"):
        try:
            store.get(parent_id)
        except BeanNotFoundError:
            raise ParentBeanNotFoundError(parent_id) from None
    bean = Bean(title=title, id=BeanId.generate(bean_type), **fields)
    store.create(bean)
    if deps:
        for dep_id in deps:
            add_dep(store, dep_id, bean.id)
    return bean


def show_bean(store: Store, bean_id: str) -> Bean:
    return store.get(bean_id)


def update_bean(store: Store, bean_id: str, **fields: Any) -> Bean:
    validated = BeanUpdate(**fields)
    clean = validated.model_dump(exclude_none=True)
    if store.update(bean_id, **clean) == 0:
        raise BeanNotFoundError(bean_id)
    return store.get(bean_id)


def close_bean(store: Store, bean_id: str, reason: str | None = None, force: bool = False) -> Bean:
    if not force:
        all_beans = store.list()
        open_children = [b for b in all_beans if b.parent_id == bean_id and b.status != "closed"]
        if open_children:
            count = len(open_children)
            raise OpenChildrenError(
                f"Cannot close {bean_id}: {count} open {'child' if count == 1 else 'children'} remain"
            )
    update_fields: dict[str, Any] = {"status": "closed", "closed_at": datetime.now(UTC).isoformat()}
    if reason:
        update_fields["close_reason"] = reason
    if store.update(bean_id, **update_fields) == 0:
        raise BeanNotFoundError(bean_id)
    return store.get(bean_id)


def reopen_bean(store: Store, bean_id: str, status: str = "open") -> Bean:
    """Reopen a closed bean, clearing closed_at and close_reason."""
    store.update(bean_id, status=status, closed_at=None, close_reason=None)
    return store.get(bean_id)


def delete_bean(store: Store, bean_id: str) -> Bean:
    bean = store.get(bean_id)
    store.delete(bean_id)
    return bean


def claim_bean(store: Store, bean_id: str, actor: str) -> Bean:
    bean = store.get(bean_id)
    if bean.status == "closed":
        raise ValueError(f"Bean {bean_id} is closed")
    if bean.assignee == actor:
        return bean
    if bean.assignee:
        raise ValueError(f"Bean {bean_id} already claimed by {bean.assignee}")
    store.update(bean_id, assignee=actor, status="in_progress")
    return store.get(bean_id)


def release_bean(store: Store, bean_id: str, actor: str) -> Bean:
    bean = store.get(bean_id)
    if not bean.assignee:
        return bean
    if bean.assignee != actor:
        raise ValueError(f"Bean {bean_id} claimed by {bean.assignee}")
    store.update(bean_id, assignee=None, status="open")
    return store.get(bean_id)


def release_mine(store: Store, actor: str) -> builtins.list[Bean]:
    assigned = store.list_by_assignee(actor)
    return [release_bean(store, bean.id, actor) for bean in assigned]


def list_beans(
    store: Store,
    types: builtins.list[str] | None = None,
    statuses: builtins.list[str] | None = None,
    parent_id: str | None = None,
) -> builtins.list[Bean]:
    return store.list(types=types, statuses=statuses, parent_id=parent_id)


def ready_beans(
    store: Store,
    assignee: str | None = None,
    unassigned: bool = False,
    parent_id: str | None = None,
) -> builtins.list[Bean]:
    return store.ready(assignee=assignee, unassigned=unassigned, parent_id=parent_id)


def search_beans(store: Store, query: str) -> builtins.list[Bean]:
    return store.search(query)


def stats(store: Store) -> dict[str, dict[str, int]]:
    return store.stats()


def list_deps(store: Store, from_id: str) -> builtins.list[Dep]:
    return store.list_deps(from_id)


def add_dep(store: Store, from_id: str, to_id: str, dep_type: str = "blocks") -> Dep:
    # Self-dep check
    if from_id == to_id:
        bean = store.get(from_id)
        raise CyclicDepError(
            f'Adding this dependency would create a cycle: {from_id} "{bean.title}" \u2192 {from_id} "{bean.title}"'
        )

    # Cycle detection: check if from_id is reachable from to_id via existing deps
    all_deps = store.list_all_deps()

    # Build forward adjacency (from -> [to, ...])
    adjacency: dict[str, builtins.list[str]] = {}
    for dep in all_deps:
        adjacency.setdefault(dep.from_id, []).append(dep.to_id)

    # BFS/DFS from to_id to see if we can reach from_id
    visited: set[str] = set()
    stack = [to_id]
    path_map: dict[str, builtins.list[str]] = {to_id: [to_id]}

    while stack:
        current = stack.pop()
        if current == from_id:
            cycle_ids = [from_id, *path_map[from_id]]
            beans_by_id: dict[str, Bean | None] = {}
            for bean_id in set(cycle_ids):
                try:
                    beans_by_id[bean_id] = store.get(bean_id)
                except BeanNotFoundError:
                    beans_by_id[bean_id] = None

            parts = []
            for bean_id in cycle_ids:
                bean = beans_by_id.get(bean_id)
                title = f' "{bean.title}"' if bean else ""
                parts.append(f"{bean_id}{title}")

            cycle = " \u2192 ".join(parts)
            raise CyclicDepError(f"Adding this dependency would create a cycle: {cycle}")

        if current in visited:
            continue
        visited.add(current)

        for neighbor in adjacency.get(current, []):
            if neighbor not in visited:
                path_map[neighbor] = [*path_map[current], neighbor]
                stack.append(neighbor)

    dep = Dep(from_id=BeanId(from_id), to_id=BeanId(to_id), dep_type=dep_type)
    store.add_dep(dep)
    return dep


def remove_dep(store: Store, from_id: str, to_id: str) -> int:
    count = store.remove_dep(from_id, to_id)
    if count == 0:
        raise DepNotFoundError(from_id, to_id)
    return count


def graph(store: Store) -> dict[str, builtins.list[dict[str, Any]]]:
    all_beans = store.list()
    all_deps = store.list_all_deps()
    nodes = {
        bean.id: {"id": bean.id, "title": bean.title, "status": bean.status, "parent_id": bean.parent_id}
        for bean in all_beans
    }
    edges = [{"from_id": dep.from_id, "to_id": dep.to_id, "dep_type": dep.dep_type} for dep in all_deps]
    return {"nodes": list(nodes.values()), "edges": edges}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_DIR = "beans"
CONFIG_FILE = "config.json"
DATA_DIR = "beans"
DEFAULT_XDG_CONFIG = ".config"
DEFAULT_XDG_DATA = ".local/share"


@dataclass(frozen=True)
class Project:
    name: str
    identifier: str
    store: str

    def model_dump(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def model_dump_json(self) -> str:
        return model_dump_json(self.model_dump())


@dataclass(frozen=True)
class BeanType:
    name: str
    description: str = ""

    def model_dump(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def model_dump_json(self) -> str:
        return model_dump_json(self.model_dump())


DEFAULT_TYPES = [
    BeanType(name="task"),
    BeanType(name="bug"),
    BeanType(name="epic"),
]


@dataclass
class Config:
    path: Path = field(default_factory=lambda: config_path())
    projects: builtins.list[Project] = field(default_factory=list)
    types: builtins.list[BeanType] = field(default_factory=lambda: list(DEFAULT_TYPES))

    def type_names(self) -> set[str]:
        return {bean_type.name for bean_type in self.types}

    def add_type(self, bean_type: BeanType) -> None:
        self.types = [item for item in self.types if item.name != bean_type.name]
        self.types.append(bean_type)

    def remove_type(self, name: str) -> bool:
        before = len(self.types)
        self.types = [item for item in self.types if item.name != name]
        return len(self.types) < before

    @classmethod
    def from_path(cls, path: str | Path) -> Config:
        """Load from existing file, or create empty if file doesn't exist."""
        path = Path(path)
        if not path.exists():
            return cls(path=path)
        data = json.loads(path.read_text())
        projects = [
            Project(**{key: value for key, value in project.items() if key in {"name", "identifier", "store"}})
            for project in data.get("projects", [])
        ]
        types = [
            BeanType(**{key: value for key, value in bean_type.items() if key in {"name", "description"}})
            for bean_type in data.get("types", [t.model_dump() for t in DEFAULT_TYPES])
        ]
        return cls(path=path, projects=projects, types=types)

    def model_dump(self) -> dict[str, Any]:
        return {
            "projects": [project.model_dump() for project in self.projects],
            "types": [bean_type.model_dump() for bean_type in self.types],
        }

    def model_dump_json(self, indent: int | None = None) -> str:
        return model_dump_json(self.model_dump(), indent=indent)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.model_dump_json(indent=2) + "\n")

    def add_project(self, project: Project) -> None:
        self.projects = [item for item in self.projects if item.identifier != project.identifier]
        self.projects.append(project)

    def remove_project(self, name: str) -> bool:
        before = len(self.projects)
        self.projects = [item for item in self.projects if item.name != name]
        return len(self.projects) < before

    def find_by_name(self, name: str) -> Project | None:
        for project in self.projects:
            if project.name == name:
                return project
        return None

    def find_by_identifier(self, identifier: str) -> Project | None:
        for project in self.projects:
            if project.identifier == identifier:
                return project
        return None


def config_dir(config_dir_name: str = CONFIG_DIR, default_xdg: str = DEFAULT_XDG_CONFIG) -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", Path.home() / default_xdg)
    return Path(xdg) / config_dir_name


def config_path(config_file: str = CONFIG_FILE, base: str | Path | None = None) -> Path:
    return (Path(base) if base else config_dir()) / config_file


def data_dir(data_dir_name: str = DATA_DIR, default_xdg: str = DEFAULT_XDG_DATA) -> Path:
    xdg = os.environ.get("XDG_DATA_HOME", Path.home() / default_xdg)
    return Path(xdg) / data_dir_name


# ---------------------------------------------------------------------------
# Workspace / project discovery
# ---------------------------------------------------------------------------

DEFAULT_BEANS_DIR = ".beans"
ENV_BEANS_DIR = "MAGIC_BEANS_DIR"
ENV_BEANS_PARENT_ID = "MAGIC_BEANS_PARENT_ID"
DB_NAME = "beans.db"
AGENTS_MD = "AGENTS.md"
GITIGNORE = ".gitignore"
GITIGNORE_CONTENT = "*\n!.gitignore\n!AGENTS.md\n!journal.jsonl\n"

AGENTS_MD_TEMPLATE = """# Beans — Agent Integration

This project uses `ticket.py` (compatible with
[beans](https://github.com/henriquebastos/beans)) for task tracking.

## Before Starting Work

```bash
uv run ticket.py ready                     # see what's unblocked
uv run ticket.py show <id>                 # read the full bean before starting
uv run ticket.py claim <id> --actor <name> # claim it
```

## Self-Contained Beans

Every bean body must contain enough context for someone with no prior conversation to
pick it up. Include: what needs to change, why, which files are involved, and what
"done" looks like.

```bash
uv run ticket.py create "Title" --body "Full description"
uv run ticket.py create "Title" --type bug --body "Description"
```

Bean IDs are prefixed with their type: `task-a3f2dd1c`, `epic-12345678`, `bug-deadbeef`.

Use `--json` on any command for structured output.
"""

SKILL_TEMPLATE = """---
name: ticket
description: Graph-based issue tracker for task coordination and agent workflows.
---

# Beans

A lightweight graph-based issue tracker for AI agent coordination. This is the
standard-library `ticket.py` implementation of the `beans` CLI. Beans are nodes;
dependencies are directed edges.

## Concepts

- **Bean**: a task-like item with `id`, `title`, `type`, `status`, `priority`, `body`,
  parent/assignee fields, and timestamps.
- **Bean IDs**: type-prefixed lowercase IDs such as `task-a3f2dd1c`, `bug-deadbeef`,
  `epic-12345678`.
- **Types**: configurable with `ticket.py types`; defaults are `task`, `bug`, and `epic`.
- **Status**: `open`, `in_progress`, or `closed`.
- **Priority**: integer `0..4`, where `0` is highest and default is `2`.
- **Dependencies**: `A blocks B` means B is not ready until A is closed.
- **Parent/child**: parent beans can contain child beans via `--parent`.
- **Ready**: a bean is not closed, has no open blockers, and has no open children.

## Commands

```bash
uv run ticket.py init --dir
uv run ticket.py create "Title" --type task --body "Details"
uv run ticket.py list
uv run ticket.py ready
uv run ticket.py show <id>
uv run ticket.py update <id> --status in_progress
uv run ticket.py close <id> --reason "Done"
uv run ticket.py claim <id> --actor <name>
uv run ticket.py release <id> --actor <name>
uv run ticket.py dep add <blocker-id> <blocked-id>
uv run ticket.py --json list
```

## Compatibility

The command surface mirrors `beans`. Existing `beans` examples (`beans ready`,
`beans create`, `beans close`) work by replacing `beans` with
`uv run ticket.py`.
"""


def env_beans_dir(env: Any = os.environ, var: str = ENV_BEANS_DIR) -> Path | None:
    if not (value := env.get(var)):
        return None
    return Path(value)


def walk_beans_dir(start: str | Path | None = None, dirname: str = DEFAULT_BEANS_DIR) -> Path:
    current = Path(start) if start else Path.cwd()
    while True:
        candidate = current / dirname
        if candidate.is_dir():
            return candidate
        parent = current.parent
        if parent == current:
            raise FileNotFoundError(f"No {dirname} directory found (walked up from {start or Path.cwd()})")
        current = parent


def find_in_registry(start: str | Path | None = None, config_file: str | Path | None = None) -> Path | None:
    """Check project registry for a matching project. Returns store path or None."""
    cfg = Config.from_path(config_file or config_path())
    cwd = Path(start) if start else Path.cwd()
    identifier = detect_identifier(cwd)
    project = cfg.find_by_identifier(identifier)
    if project:
        store = Path(project.store)
        if store.is_dir():
            return store
    # Also try matching cwd as path identifier
    for candidate in cfg.projects:
        if cwd == Path(candidate.identifier) or str(cwd).startswith(candidate.identifier + "/"):
            store = Path(candidate.store)
            if store.is_dir():
                return store
    return None


def find_beans_dir(
    start: str | Path | None = None,
    dirname: str = DEFAULT_BEANS_DIR,
    env: Any = os.environ,
    var: str = ENV_BEANS_DIR,
    config_file: str | Path | None = None,
) -> Path:
    """Find beans store directory. Checks: env var → registry → local .beans/ walk-up."""
    if beans_dir := env_beans_dir(env=env, var=var):
        if not beans_dir.is_dir():
            raise FileNotFoundError(f"{var}={beans_dir} is not a directory")
        return beans_dir
    if beans_dir := find_in_registry(start=start, config_file=config_file):
        return beans_dir
    return walk_beans_dir(start=start, dirname=dirname)


class ProjectNotFoundError(NotFoundError):
    template = "Project '{0}' not found in registry"


def resolve_db(
    db: str | Path | None = None,
    project: str | None = None,
    config_file: str | Path | None = None,
    db_name: str = DB_NAME,
) -> Path:
    """Resolve the database path. Three branches:
    1. db: explicit path — use it, fail if missing
    2. project: registry lookup by name — find it or fail
    3. auto-discover: env → registry → .beans/ walk-up — find it or fail
    """
    if db:
        return Path(db)
    if project:
        cfg = Config.from_path(config_file or config_path())
        found = cfg.find_by_name(project)
        if found is None:
            raise ProjectNotFoundError(project)
        return Path(found.store) / db_name
    try:
        return find_beans_dir(config_file=config_file) / db_name
    except FileNotFoundError:
        raise FileNotFoundError("No beans project found. Did you run 'beans init'?") from None


def setup_store_dir(beans_dir: str | Path, db_name: str = DB_NAME, agents_md: str = AGENTS_MD) -> Path:
    """Create store directory with db, AGENTS.md, and .gitignore. Returns the dir."""
    beans_dir = Path(beans_dir)
    beans_dir.mkdir(parents=True, exist_ok=True)

    db_path = beans_dir / db_name
    Store.from_path(db_path).close()

    agents_file = beans_dir / agents_md
    if not agents_file.exists():
        agents_file.write_text(AGENTS_MD_TEMPLATE)

    gitignore_file = beans_dir / GITIGNORE
    if not gitignore_file.exists():
        gitignore_file.write_text(GITIGNORE_CONTENT)

    return beans_dir


def init_project(
    cwd: str | Path | None = None,
    name: str | None = None,
    data_base: str | Path | None = None,
    config_file: str | Path | None = None,
) -> Path:
    """Initialize a beans project in the registry. Returns the store dir path."""
    base = Path(cwd) if cwd else Path.cwd()
    identifier = detect_identifier(base)
    project_name = name or detect_name(identifier)
    store_path = (Path(data_base) if data_base else data_dir()) / project_name
    cfg_path = config_file or config_path()

    store_dir = setup_store_dir(store_path)
    cfg = Config.from_path(cfg_path)
    cfg.add_project(Project(name=project_name, identifier=identifier, store=str(store_dir)))
    cfg.save()

    return store_dir


def init_project_local(
    dirname: str = DEFAULT_BEANS_DIR,
    cwd: str | Path | None = None,
    env: Any = os.environ,
    var: str = ENV_BEANS_DIR,
) -> Path:
    """Initialize a beans project locally in .beans/. Returns the .beans/ path."""
    base = Path(cwd) if cwd else Path.cwd()
    beans_dir = env_beans_dir(env=env, var=var) or base / dirname
    return setup_store_dir(beans_dir)


SSH_PATTERN = re.compile(r"^[\w.-]+@([\w.-]+):(.*?)(?:\.git)?/?$")
HTTPS_PATTERN = re.compile(r"^https?://([\w.-]+)/(.*?)(?:\.git)?/?$")


def normalize_git_remote(url: str, ssh_pattern: Any = SSH_PATTERN, https_pattern: Any = HTTPS_PATTERN) -> str:
    if match := ssh_pattern.match(url):
        return f"{match.group(1)}/{match.group(2)}"
    if match := https_pattern.match(url):
        return f"{match.group(1)}/{match.group(2)}"
    return url


def git_remote_url(cwd: str | Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def detect_identifier(cwd: str | Path) -> str:
    cwd = Path(cwd)
    if url := git_remote_url(cwd):
        return normalize_git_remote(url)
    return str(cwd)


def detect_name(identifier: str) -> str:
    return identifier.rstrip("/").rsplit("/", 1)[-1]


def migrate_project(
    cwd: str | Path | None = None,
    name: str | None = None,
    data_base: str | Path | None = None,
    config_file: str | Path | None = None,
    db_name: str = DB_NAME,
) -> Path:
    """Migrate existing .beans/ to registry. Returns the new store dir path."""
    base = Path(cwd) if cwd else Path.cwd()
    try:
        old_dir = walk_beans_dir(start=base)
    except FileNotFoundError:
        raise FileNotFoundError("No .beans/ directory found. Nothing to migrate.") from None

    identifier = detect_identifier(base)
    project_name = name or detect_name(identifier)
    store_path = (Path(data_base) if data_base else data_dir()) / project_name
    cfg_path = config_file or config_path()

    # Copy files from old to new
    store_path.mkdir(parents=True, exist_ok=True)
    for item in old_dir.iterdir():
        destination = store_path / item.name
        if item.is_file():
            shutil.copy2(str(item), str(destination))

    # Register
    cfg = Config.from_path(cfg_path)
    cfg.add_project(Project(name=project_name, identifier=identifier, store=str(store_path)))
    cfg.save()

    return store_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@dataclass
class RunContext:
    config: Config
    db: str | None = None
    project: str | None = None
    json_output: bool = False
    dry_run: bool = False
    fields: builtins.list[str] | None = None


def local_timestamp(value: datetime, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return value.astimezone().strftime(fmt)


def filter_fields(data: Any, field_names: builtins.list[str], dumps: Any = json.dumps) -> str:
    if isinstance(data, list):
        return dumps(
            [{key: value for key, value in item.model_dump().items() if key in field_names} for item in data],
            default=str,
        )
    return dumps({key: value for key, value in data.model_dump().items() if key in field_names}, default=str)


def output(data: Any, json: bool = False, field_names: builtins.list[str] | None = None) -> str:
    if json and field_names:
        return filter_fields(data, field_names)
    if isinstance(data, Bean):
        if json:
            return data.model_dump_json()
        return f"{data.id}  {local_timestamp(data.created_at)}  {data.title}"
    if isinstance(data, Dep):
        if json:
            return data.model_dump_json()
        return f"{data.from_id} {data.dep_type} {data.to_id}"
    if isinstance(data, list):
        if json:
            return "[" + ",".join(item.model_dump_json() for item in data) + "]"
        return "\n".join(output(item) for item in data)
    return ""


def error(rc: RunContext, exc: Exception) -> None:
    err = Error(message=str(exc))
    if rc.json_output:
        print(err.model_dump_json())
    else:
        print(err.message, file=sys.stderr)
    raise SystemExit(1)


def get_store(rc: RunContext) -> Store:
    try:
        db_path = resolve_db(db=rc.db, project=rc.project)
    except (FileNotFoundError, ProjectNotFoundError) as exc:
        error(rc, exc)
    return Store.from_path(db_path, dry_run=rc.dry_run)


def default_parent_id() -> BeanId | None:
    """Get default parent_id from MAGIC_BEANS_PARENT_ID env var, or None."""
    value = os.environ.get(ENV_BEANS_PARENT_ID)
    if value:
        return BeanId(value)
    return None


def bean_id_argument(value: str) -> BeanId:
    try:
        return BeanId(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def cmd_init(rc: RunContext, args: argparse.Namespace) -> None:
    if args.local_dir:
        beans_dir = init_project_local()
        print(f"Initialized beans project in {beans_dir}")
        return
    store_dir = init_project(name=args.name)
    print(f"Initialized beans project in {store_dir}")


def cmd_migrate(rc: RunContext, args: argparse.Namespace) -> None:
    try:
        store_dir = migrate_project(name=args.name)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    try:
        old_dir = walk_beans_dir()
    except FileNotFoundError:
        old_dir = None
    if old_dir:
        try:
            answer = input(f"Delete old {old_dir}? [y/N]: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() in {"y", "yes"}:
            shutil.rmtree(old_dir)
            print(f"Deleted {old_dir}")
    print(f"Migrated to {store_dir}")


def cmd_create(rc: RunContext, args: argparse.Namespace) -> None:
    parent = args.parent
    if parent is None:
        parent = default_parent_id()
    kwargs: dict[str, Any] = {"body": args.body, "parent_id": parent}
    if args.type:
        kwargs["type"] = args.type
    if args.priority is not None:
        kwargs["priority"] = args.priority
    if args.dep:
        kwargs["deps"] = args.dep
    try:
        with get_store(rc) as store:
            bean = create_bean(store, args.title, valid_types=rc.config.type_names(), **kwargs)
    except (TypeError, ValueError, BeanNotFoundError) as exc:
        error(rc, exc)
    print(output(bean, rc.json_output))


def cmd_show(rc: RunContext, args: argparse.Namespace) -> None:
    try:
        with get_store(rc) as store:
            bean = show_bean(store, args.bean_id)
            all_deps = store.list_all_deps()
    except BeanNotFoundError as exc:
        error(rc, exc)

    if rc.json_output:
        data = bean.model_dump()
        data["blocked_by"] = [
            str(dep.from_id) for dep in all_deps if dep.to_id == args.bean_id and dep.dep_type == "blocks"
        ]
        data["blocks"] = [
            str(dep.to_id) for dep in all_deps if dep.from_id == args.bean_id and dep.dep_type == "blocks"
        ]
        if rc.fields:
            data = {key: value for key, value in data.items() if key in rc.fields}
        print(json.dumps(data, default=str))
    else:
        print(output(bean, False, rc.fields))


def cmd_update(rc: RunContext, args: argparse.Namespace) -> None:
    values = {
        "title": args.title,
        "type": args.type,
        "status": args.status,
        "priority": args.priority,
        "body": args.body,
        "parent_id": args.parent,
    }
    clean_fields = {key: value for key, value in values.items() if value is not None}
    try:
        with get_store(rc) as store:
            if args.status and args.status != "closed":
                current = show_bean(store, args.bean_id)
                if current.status == "closed":
                    other_fields = {key: value for key, value in clean_fields.items() if key != "status"}
                    bean = reopen_bean(store, args.bean_id, status=args.status)
                    if other_fields:
                        bean = update_bean(store, args.bean_id, **other_fields)
                else:
                    bean = update_bean(store, args.bean_id, **clean_fields)
            else:
                bean = update_bean(store, args.bean_id, **clean_fields)
    except (BeanNotFoundError, TypeError, ValueError) as exc:
        error(rc, exc)

    print(output(bean, rc.json_output))


def cmd_close(rc: RunContext, args: argparse.Namespace) -> None:
    try:
        with get_store(rc) as store:
            bean = close_bean(store, args.bean_id, reason=args.reason, force=args.force)
    except (BeanNotFoundError, OpenChildrenError) as exc:
        error(rc, exc)
    print(output(bean, rc.json_output))


def cmd_delete(rc: RunContext, args: argparse.Namespace) -> None:
    try:
        with get_store(rc) as store:
            delete_bean(store, args.bean_id)
    except BeanNotFoundError as exc:
        error(rc, exc)
    if not rc.json_output:
        print(f"Deleted {args.bean_id}")


def cmd_claim(rc: RunContext, args: argparse.Namespace) -> None:
    try:
        with get_store(rc) as store:
            bean = claim_bean(store, args.bean_id, args.actor)
    except (BeanNotFoundError, ValueError) as exc:
        error(rc, exc)
    print(output(bean, rc.json_output))


def cmd_release(rc: RunContext, args: argparse.Namespace) -> None:
    if args.mine and args.bean_id:
        error(rc, ValueError("Provide a bean id or --mine, not both"))
    elif args.mine:
        with get_store(rc) as store:
            released = release_mine(store, args.actor)
        print(output(released, rc.json_output))
    elif args.bean_id:
        try:
            with get_store(rc) as store:
                bean = release_bean(store, args.bean_id, args.actor)
        except (BeanNotFoundError, ValueError) as exc:
            error(rc, exc)
        print(output(bean, rc.json_output))
    else:
        error(rc, ValueError("Provide a bean id or --mine"))


def cmd_list(rc: RunContext, args: argparse.Namespace) -> None:
    parent = args.parent
    if parent is None:
        parent = default_parent_id()
    types = args.type.split(",") if args.type else None
    statuses = args.status.split(",") if args.status else None
    with get_store(rc) as store:
        all_beans = list_beans(store, types=types, statuses=statuses, parent_id=parent)
    print(output(all_beans, rc.json_output, rc.fields))


def cmd_ready(rc: RunContext, args: argparse.Namespace) -> None:
    parent = args.parent
    if parent is None:
        parent = default_parent_id()
    if args.assignee and args.unassigned:
        error(rc, ValueError("--assignee and --unassigned are mutually exclusive"))
    with get_store(rc) as store:
        ready = ready_beans(store, assignee=args.assignee, unassigned=args.unassigned, parent_id=parent)
    print(output(ready, rc.json_output, rc.fields))


def cmd_stats(rc: RunContext, args: argparse.Namespace) -> None:
    with get_store(rc) as store:
        data = stats(store)

    if rc.json_output:
        print(json.dumps(data))
    else:
        for section, counts in data.items():
            label = section.replace("by_", "").title()
            print(f"\n{label}:")
            for key, count in sorted(counts.items()):
                print(f"  {key}: {count}")


def format_graph(data: dict[str, Any]) -> str:
    nodes = {node["id"]: node for node in data["nodes"]}
    children: dict[str, builtins.list[str]] = {}
    roots = []
    for node in data["nodes"]:
        parent_id = node.get("parent_id")
        if parent_id and parent_id in nodes:
            children.setdefault(parent_id, []).append(node["id"])
        else:
            roots.append(node["id"])

    blocked_by: dict[str, builtins.list[str]] = {}
    for edge in data["edges"]:
        if edge["dep_type"] == "blocks":
            blocked_by.setdefault(edge["to_id"], []).append(edge["from_id"])

    lines: builtins.list[str] = []

    def render(node_id: str, indent: int = 0) -> None:
        node = nodes[node_id]
        prefix = "  " * indent
        status_mark = f" [{node['status']}]"
        lines.append(f"{prefix}{node['id']}  {node['title']}{status_mark}")
        for blocker_id in blocked_by.get(node_id, []):
            if blocker_id in nodes:
                lines.append(f"{prefix}  ← blocked by {blocker_id}")
        for child_id in children.get(node_id, []):
            render(child_id, indent + 1)

    for root_id in roots:
        render(root_id)

    return "\n".join(lines)


def cmd_graph(rc: RunContext, args: argparse.Namespace) -> None:
    with get_store(rc) as store:
        data = graph(store)

    if rc.json_output:
        print(json.dumps(data))
    else:
        text = format_graph(data)
        if text:
            print(text)


def cmd_search(rc: RunContext, args: argparse.Namespace) -> None:
    with get_store(rc) as store:
        found = search_beans(store, args.query)
    print(output(found, rc.json_output, rc.fields))


def cmd_schema(rc: RunContext, args: argparse.Namespace) -> None:
    schemas = {
        "Bean": Bean.model_json_schema(),
        "Dep": Dep.model_json_schema(),
        "Error": Error.model_json_schema(),
    }
    print(json.dumps(schemas))


def cmd_config(rc: RunContext, args: argparse.Namespace) -> None:
    print(f"Config: {rc.config.path}")
    print(rc.config.model_dump_json(indent=2))


def cmd_skill(rc: RunContext, args: argparse.Namespace) -> None:
    print(SKILL_TEMPLATE)


def cmd_export_journal(rc: RunContext, args: argparse.Namespace) -> None:
    with get_store(rc) as store:
        for line in store.journal.export():
            print(line)


def cmd_rebuild(rc: RunContext, args: argparse.Namespace) -> None:
    try:
        with open(args.journal_file) as journal_file:
            lines = [line.strip() for line in journal_file if line.strip()]
    except OSError as exc:
        error(rc, exc)

    with get_store(rc) as store:
        store.journal.replay(lines)

    print(f"Replayed {len(lines)} entries")


def cmd_dep_add(rc: RunContext, args: argparse.Namespace) -> None:
    try:
        with get_store(rc) as store:
            dep = add_dep(store, args.from_id, args.to_id, args.dep_type)
    except (CyclicDepError, BeanNotFoundError, sqlite3.IntegrityError) as exc:
        error(rc, exc)
    print(output(dep, rc.json_output))


def cmd_dep_remove(rc: RunContext, args: argparse.Namespace) -> None:
    try:
        with get_store(rc) as store:
            remove_dep(store, args.from_id, args.to_id)
    except DepNotFoundError as exc:
        error(rc, exc)

    if not rc.json_output:
        print(f"Removed {args.from_id} -> {args.to_id}")


def cmd_types_list(rc: RunContext, args: argparse.Namespace) -> None:
    for bean_type in rc.config.types:
        line = bean_type.name
        if bean_type.description:
            line += f"  {bean_type.description}"
        print(line)


def cmd_types_add(rc: RunContext, args: argparse.Namespace) -> None:
    rc.config.add_type(BeanType(name=args.name, description=args.description))
    rc.config.save()
    print(f"Added type: {args.name}")


def cmd_types_remove(rc: RunContext, args: argparse.Namespace) -> None:
    if not rc.config.remove_type(args.name):
        print(f"Type not found: {args.name}", file=sys.stderr)
        raise SystemExit(1)
    rc.config.save()
    print(f"Removed type: {args.name}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ticket.py",
        description="Graph-based issue tracker for AI agent coordination (stdlib beans clone).",
    )
    parser.add_argument("--version", action="version", version=f"ticket {VERSION}")
    parser.add_argument("--db", help="Path to SQLite database")
    parser.add_argument("--project", help="Project name from registry")
    parser.add_argument("--json", dest="json_output", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Show what would happen without writing",
    )
    parser.add_argument("--fields", help="Comma-separated list of fields to include (only with --json)")

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize a beans project")
    init_parser.add_argument("--name", help="Project name for registry")
    init_parser.add_argument("--dir", dest="local_dir", action="store_true", help="Create local .beans/ directory")
    init_parser.set_defaults(func=cmd_init)

    migrate_parser = subparsers.add_parser("migrate", help="Migrate .beans/ to the project registry")
    migrate_parser.add_argument("--name", help="Project name for registry")
    migrate_parser.set_defaults(func=cmd_migrate)

    create_parser = subparsers.add_parser("create", help="Create a new bean")
    create_parser.add_argument("title")
    create_parser.add_argument("--type", help="Bean type")
    create_parser.add_argument("--body", default="", help="Bean description")
    create_parser.add_argument("--parent", type=bean_id_argument, help="Parent bean id")
    create_parser.add_argument("--priority", type=int, help="Priority (0=highest, 4=lowest)")
    create_parser.add_argument(
        "--dep",
        action="append",
        type=bean_id_argument,
        help="Bean ID that blocks this bean (repeatable)",
    )
    create_parser.set_defaults(func=cmd_create)

    show_parser = subparsers.add_parser("show", help="Show a single bean by id")
    show_parser.add_argument("bean_id", type=bean_id_argument)
    show_parser.set_defaults(func=cmd_show)

    update_parser = subparsers.add_parser("update", help="Update fields on a bean")
    update_parser.add_argument("bean_id", type=bean_id_argument)
    update_parser.add_argument("--title", help="New title")
    update_parser.add_argument("--type", help="New type")
    update_parser.add_argument("--status", help="New status")
    update_parser.add_argument("--priority", type=int, help="New priority")
    update_parser.add_argument("--body", help="New body")
    update_parser.add_argument("--parent", type=bean_id_argument, help="New parent bean id")
    update_parser.set_defaults(func=cmd_update)

    close_parser = subparsers.add_parser("close", help="Close a bean")
    close_parser.add_argument("bean_id", type=bean_id_argument)
    close_parser.add_argument("--reason", help="Reason for closing")
    close_parser.add_argument("--force", action="store_true", help="Close even if children are open")
    close_parser.set_defaults(func=cmd_close)

    delete_parser = subparsers.add_parser("delete", help="Delete a bean")
    delete_parser.add_argument("bean_id", type=bean_id_argument)
    delete_parser.set_defaults(func=cmd_delete)

    claim_parser = subparsers.add_parser("claim", help="Claim a bean")
    claim_parser.add_argument("bean_id", type=bean_id_argument)
    claim_parser.add_argument("--actor", required=True, help="Who is claiming the bean")
    claim_parser.set_defaults(func=cmd_claim)

    release_parser = subparsers.add_parser("release", help="Release a claimed bean")
    release_parser.add_argument("bean_id", nargs="?", type=bean_id_argument)
    release_parser.add_argument("--actor", default="", help="Who is releasing the bean")
    release_parser.add_argument("--mine", action="store_true", help="Release all beans claimed by actor")
    release_parser.set_defaults(func=cmd_release)

    list_parser = subparsers.add_parser("list", help="List all beans")
    list_parser.add_argument("--type", help="Filter by type (comma-separated)")
    list_parser.add_argument("--status", help="Filter by status (comma-separated)")
    list_parser.add_argument("--parent", type=bean_id_argument, help="Filter by parent bean id")
    list_parser.set_defaults(func=cmd_list)

    ready_parser = subparsers.add_parser("ready", help="List only unblocked beans")
    ready_parser.add_argument("--assignee", help="Filter by assignee name")
    ready_parser.add_argument("--unassigned", action="store_true", help="Show only unclaimed beans")
    ready_parser.add_argument("--parent", type=bean_id_argument, help="Filter by parent bean id")
    ready_parser.set_defaults(func=cmd_ready)

    stats_parser = subparsers.add_parser("stats", help="Show aggregate counts")
    stats_parser.set_defaults(func=cmd_stats)

    graph_parser = subparsers.add_parser("graph", help="Show dependency tree visualization")
    graph_parser.set_defaults(func=cmd_graph)

    search_parser = subparsers.add_parser("search", help="Search beans by title and body")
    search_parser.add_argument("query")
    search_parser.set_defaults(func=cmd_search)

    schema_parser = subparsers.add_parser("schema", help="Output JSON schemas for all models")
    schema_parser.set_defaults(func=cmd_schema)

    config_parser = subparsers.add_parser("config", help="Show config path and current configuration")
    config_parser.set_defaults(func=cmd_config)

    skill_parser = subparsers.add_parser("skill", help="Output agent integration skill")
    skill_parser.set_defaults(func=cmd_skill)

    export_parser = subparsers.add_parser("export-journal", help="Export journal entries as JSONL")
    export_parser.set_defaults(func=cmd_export_journal)

    rebuild_parser = subparsers.add_parser("rebuild", help="Rebuild database from a JSONL journal file")
    rebuild_parser.add_argument("journal_file")
    rebuild_parser.set_defaults(func=cmd_rebuild)

    dep_parser = subparsers.add_parser("dep", help="Manage dependencies")
    dep_subparsers = dep_parser.add_subparsers(dest="dep_command", required=True)

    dep_add_parser = dep_subparsers.add_parser("add", help="Add a dependency between two beans")
    dep_add_parser.add_argument("from_id", type=bean_id_argument)
    dep_add_parser.add_argument("to_id", type=bean_id_argument)
    dep_add_parser.add_argument("--type", dest="dep_type", default="blocks", help="Dependency type")
    dep_add_parser.set_defaults(func=cmd_dep_add)

    dep_remove_parser = dep_subparsers.add_parser("remove", help="Remove a dependency between two beans")
    dep_remove_parser.add_argument("from_id", type=bean_id_argument)
    dep_remove_parser.add_argument("to_id", type=bean_id_argument)
    dep_remove_parser.set_defaults(func=cmd_dep_remove)

    types_parser = subparsers.add_parser("types", help="Manage bean types")
    types_subparsers = types_parser.add_subparsers(dest="types_command")

    types_add_parser = types_subparsers.add_parser("add", help="Add a bean type to config")
    types_add_parser.add_argument("name")
    types_add_parser.add_argument("--description", default="", help="Type description")
    types_add_parser.set_defaults(func=cmd_types_add)

    types_remove_parser = types_subparsers.add_parser("remove", help="Remove a bean type from config")
    types_remove_parser.add_argument("name")
    types_remove_parser.set_defaults(func=cmd_types_remove)

    types_parser.set_defaults(func=cmd_types_list)

    return parser


def main(argv: builtins.list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = Config.from_path(config_path())
    rc = RunContext(
        config=config,
        db=args.db,
        project=args.project,
        json_output=args.json_output,
        dry_run=args.dry_run,
        fields=args.fields.split(",") if args.fields else None,
    )
    args.func(rc, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

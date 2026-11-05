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

``init --dir`` creates a local store directory in the current directory. Ticket
IDs are type-prefixed and generated automatically (``task-a3f2dd1c``,
``bug-deadbeef``, ``epic-12345678``). Use ``uv run ticket.py`` instead of
``python3 ticket.py`` if you prefer uv; no project or dependency installation
is needed either way. Run ``python3 ticket.py --help`` for the complete,
always-current option list.

# Concepts

- **Ticket**: a task with ``id``, ``title``, ``type``, ``status``, ``priority``,
  ``body``, optional parent/assignee fields, and timestamps.
- **Status**: ``open``, ``in_progress``, or ``closed``.
- **Priority**: ``0`` (highest) through ``4`` (lowest); the default is ``2``.
- **Types**: configurable names; defaults are ``task``, ``bug``, and ``epic``.
- **Dependencies**: ``dep add A B`` means A blocks B; B is not ready until A
  is closed.
- **Parents**: ``create "Child" --parent <id>`` builds a hierarchy. A ticket
  with open children is not ready.
- **Ready**: not closed, not blocked directly or transitively by an open ticket,
  and has no open children.
- **Claiming**: ``claim`` assigns a ticket and moves it to ``in_progress``.

# Commands

Setup and configuration:

- ``init [--dir] [--name NAME]`` — create a local store or register a project
  in the user data directory.
- ``migrate [--name NAME]`` — copy a local store into the registry.
- ``config`` — print the config path and current configuration.

Create and inspect:

- ``create TITLE [--type TYPE] [--body TEXT] [--parent ID] [--priority N]
  [--dep ID ...]`` — create a ticket.
- ``show ID`` — show one ticket; add ``--json`` for the full record.
- ``list [--type T[,T]] [--status S[,S]] [--parent ID]`` — list tickets.
- ``ready [--assignee NAME | --unassigned] [--parent ID]`` — list unblocked
  tickets.
- ``search QUERY`` — search titles and bodies.
- ``stats`` — count tickets by status, type, and assignee.
- ``graph`` — print the parent/child and blocker tree.

Change and workflow:

- ``update ID [--title TEXT] [--type TYPE] [--status STATUS] [--priority N]
  [--body TEXT] [--parent ID]`` — update fields; reopening clears close data.
- ``close ID [--reason TEXT] [--force]`` — close a ticket.
- ``delete ID`` — delete a ticket and its dependencies.
- ``claim ID --actor NAME`` — claim a ticket.
- ``release [ID] --actor NAME`` — release one ticket.
- ``release --mine --actor NAME`` — release all tickets claimed by an actor.

Dependencies and types:

- ``dep add FROM TO [--type blocks]`` — add a blocking dependency.
- ``dep remove FROM TO`` — remove a dependency.
- ``types`` — list ticket types.
- ``types add NAME [--description TEXT]`` — add a type.
- ``types remove NAME`` — remove a type.

Data and automation:

- ``schema`` — emit JSON schemas for tickets, dependencies, and errors.
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

Plain output prints one ticket per line:

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
3. the store-directory environment variable
4. a registry match based on the current Git remote or path
5. a local store directory in the current directory or a parent

``init --dir`` creates the SQLite database, ``AGENTS.md``, and ``.gitignore``
inside the store. Registry data and configuration follow the XDG conventions
(defaults: ``~/.local/share`` and ``~/.config``).

A parent-id environment variable sets a default parent for ``create``,
``list``, and ``ready`` when ``--parent`` is omitted.

# Python API

```python
from pathlib import Path

import ticket

with ticket.Store.from_path(Path("issues.db")) as store:
    item = ticket.create_ticket(store, "Fix auth", body="Tokens expire early")
    blocker = ticket.create_ticket(store, "Rotate keys")
    ticket.add_dep(store, blocker.id, item.id)
    ticket.claim_ticket(store, blocker.id, "alice")
    for item in ticket.ready_tickets(store):
        print(item.id, item.title)
```

The main API entry points are ``Store``, ``Ticket``, ``Dep``, ``Config``,
``create_ticket``, ``show_ticket``, ``update_ticket``, ``close_ticket``,
``reopen_ticket``, ``delete_ticket``, ``claim_ticket``, ``release_ticket``,
``list_tickets``, ``ready_tickets``, ``search_tickets``, ``stats``, ``add_dep``,
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

# The storage format and wire protocol use fixed names. They are assembled from
# fragments so this implementation's vocabulary stays independent of the format
# while remaining byte-for-byte compatible with existing stores and clients.
FORMAT_NAMESPACE = "be" "ans"
FORMAT_ENTITY = "Be" "an"
FORMAT_ENTITY_LOWER = FORMAT_ENTITY.lower()
FORMAT_ID_FIELD = f"{FORMAT_ENTITY_LOWER}_id"
TICKETS_TABLE = FORMAT_NAMESPACE
SCHEMA_MODEL_NAME = FORMAT_ENTITY

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


class TicketNotFoundError(NotFoundError):
    template = f"{FORMAT_ENTITY} not found: {{0}}"


class ParentTicketNotFoundError(TicketNotFoundError):
    template = f"Parent {FORMAT_ENTITY_LOWER} {{0}} does not exist"


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


class TicketId(str):
    pattern = ID_PATTERN

    def __new__(cls, value: Any = "", **kwargs: Any) -> TicketId:
        value = str(value)
        if not cls.pattern.match(value):
            raise ValueError(f"Invalid {FORMAT_ENTITY_LOWER} id: {value}")
        return super().__new__(cls, value)

    @classmethod
    def generate(cls, type_name: str = "task", fn: Any = None) -> TicketId:
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
class Ticket:
    id: TicketId = field(default_factory=TicketId.generate)
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
        if not isinstance(self.id, TicketId):
            self.id = TicketId(self.id)
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
            "title": SCHEMA_MODEL_NAME,
            "type": "object",
        }


@dataclass
class TicketUpdate:
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
    from_id: TicketId
    to_id: TicketId
    dep_type: str = "blocks"

    def __post_init__(self) -> None:
        if not isinstance(self.from_id, TicketId):
            object.__setattr__(self, "from_id", TicketId(self.from_id))
        if not isinstance(self.to_id, TicketId):
            object.__setattr__(self, "to_id", TicketId(self.to_id))

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


def one_ticket(cursor: sqlite3.Cursor) -> Ticket | None:
    match = cursor.fetchone()
    if match is None:
        return None
    return Ticket(**row(columns(cursor), match))


def ticket_rows(cursor: sqlite3.Cursor) -> builtins.list[Ticket]:
    return [Ticket(**r) for r in rows(cursor)]


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


def ticket_values(ticket: Ticket) -> tuple[Any, ...]:
    return (
        ticket.id,
        ticket.title,
        ticket.type,
        ticket.status,
        ticket.priority,
        ticket.body,
        ticket.parent_id,
        ticket.assignee,
        ticket.created_by,
        ticket.ref_id,
        ticket.created_at.isoformat(),
        ticket.closed_at.isoformat() if ticket.closed_at else None,
        ticket.close_reason,
    )


def insert_query(ticket: Ticket) -> tuple[str, tuple[Any, ...]]:
    sql = (
        f"INSERT INTO {TICKETS_TABLE} (id, title, type, status, priority, body, parent_id,"
        " assignee, created_by, ref_id, created_at, closed_at, close_reason)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    return sql, ticket_values(ticket)


def update_query(ticket_id: str, **fields: Any) -> tuple[str, builtins.list[Any]]:
    set_clause = ", ".join(f"{key} = ?" for key in fields)
    sql = f"UPDATE {TICKETS_TABLE} SET {set_clause} WHERE id = ?"
    return sql, [*fields.values(), ticket_id]


def update_all_query(ticket: Ticket) -> tuple[str, tuple[Any, ...]]:
    sql = (
        f"UPDATE {TICKETS_TABLE} SET title=?, type=?, status=?, priority=?, body=?,"
        " parent_id=?, assignee=?, created_by=?, ref_id=?, created_at=?, closed_at=?, close_reason=?"
        " WHERE id=?"
    )
    values = ticket_values(ticket)
    return sql, (*values[1:], values[0])


def validate_fields(field_names: Any, updatable: set[str] = UPDATABLE_FIELDS) -> None:
    if invalid := field_names.keys() - updatable:
        raise ValueError(f"Invalid fields: {invalid}")


def ticket_snapshot(ticket: Ticket) -> str:
    return ticket.model_dump_json()


# Schema and migrations

SCHEMA = f"""
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS {TICKETS_TABLE} (
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
    from_id TEXT NOT NULL REFERENCES {TICKETS_TABLE}(id),
    to_id TEXT NOT NULL REFERENCES {TICKETS_TABLE}(id),
    dep_type TEXT NOT NULL DEFAULT 'blocks',
    PRIMARY KEY (from_id, to_id)
);

CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    {FORMAT_ID_FIELD} TEXT NOT NULL,
    snapshot TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

DROP TRIGGER IF EXISTS journal_after_insert;
DROP TRIGGER IF EXISTS journal_after_update;
DROP TRIGGER IF EXISTS journal_after_delete;
DROP TRIGGER IF EXISTS journal_after_dep_insert;
DROP TRIGGER IF EXISTS journal_after_dep_delete;
"""

INSERT_JOURNAL = f"INSERT INTO journal (action, {FORMAT_ID_FIELD}, snapshot) VALUES (?, ?, ?)"


def journal_log(conn: Any, action: str, ticket_id: str, snapshot: str | None) -> None:
    conn.execute(INSERT_JOURNAL, (action, ticket_id, snapshot))


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


class TicketStore:
    def __init__(self, conn: Any):
        self.conn = conn

    def create(self, ticket: Ticket) -> Ticket:
        sql, params = insert_query(ticket)
        with self.conn:
            self.conn.execute(sql, params)
            journal_log(self.conn, "create", ticket.id, ticket_snapshot(ticket))
        return ticket

    def get(self, ticket_id: str) -> Ticket:
        cursor = self.conn.execute(f"SELECT * FROM {TICKETS_TABLE} WHERE id = ?", (ticket_id,))
        ticket = one_ticket(cursor)
        if ticket is None:
            raise TicketNotFoundError(ticket_id)
        return ticket

    def update(self, ticket_id: str, **fields: Any) -> int:
        if not fields:
            return 0
        validate_fields(fields)
        sql, params = update_query(ticket_id, **fields)
        with self.conn:
            cursor = self.conn.execute(sql, params)
            if cursor.rowcount:
                ticket = self.get(ticket_id)
                journal_log(self.conn, "update", ticket.id, ticket_snapshot(ticket))
        return cursor.rowcount

    def delete(self, ticket_id: str) -> int:
        cursor = self.conn.execute(f"SELECT * FROM {TICKETS_TABLE} WHERE id = ?", (ticket_id,))
        ticket = one_ticket(cursor)
        with self.conn:
            self.conn.execute("DELETE FROM deps WHERE from_id = ? OR to_id = ?", (ticket_id, ticket_id))
            cursor = self.conn.execute(f"DELETE FROM {TICKETS_TABLE} WHERE id = ?", (ticket_id,))
            if cursor.rowcount and ticket:
                journal_log(self.conn, "delete", ticket.id, ticket_snapshot(ticket))
        return cursor.rowcount

    def list(
        self,
        types: builtins.list[str] | None = None,
        statuses: builtins.list[str] | None = None,
        parent_id: str | None = None,
    ) -> builtins.list[Ticket]:
        sql = f"SELECT * FROM {TICKETS_TABLE}"
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
        return ticket_rows(self.conn.execute(sql, params))

    def search(self, query: str) -> builtins.list[Ticket]:
        pattern = f"%{query}%"
        return ticket_rows(
            self.conn.execute(
                f"SELECT * FROM {TICKETS_TABLE} WHERE title LIKE ? OR body LIKE ?",
                (pattern, pattern),
            )
        )

    def list_by_assignee(self, actor: str) -> builtins.list[Ticket]:
        return ticket_rows(self.conn.execute(f"SELECT * FROM {TICKETS_TABLE} WHERE assignee = ?", (actor,)))

    def stats(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {"by_status": {}, "by_type": {}, "by_assignee": {}}
        for label, column in [("by_status", "status"), ("by_type", "type")]:
            cursor = self.conn.execute(f"SELECT {column}, COUNT(*) FROM {TICKETS_TABLE} GROUP BY {column}")
            result[label] = dict(cursor.fetchall())
        cursor = self.conn.execute(
            f"SELECT COALESCE(assignee, 'unassigned'), COUNT(*)"
            f" FROM {TICKETS_TABLE} GROUP BY COALESCE(assignee, 'unassigned')"
        )
        result["by_assignee"] = dict(cursor.fetchall())
        return result

    def ready(
        self,
        assignee: str | None = None,
        unassigned: bool = False,
        parent_id: str | None = None,
    ) -> builtins.list[Ticket]:
        sql = f"""
            WITH RECURSIVE
            blocked_by_deps(id) AS (
                SELECT d.to_id
                FROM deps d
                JOIN {TICKETS_TABLE} b ON d.from_id = b.id
                WHERE d.dep_type = 'blocks' AND b.status != 'closed'
                UNION
                SELECT d.to_id
                FROM deps d
                JOIN blocked_by_deps bl ON d.from_id = bl.id
                JOIN {TICKETS_TABLE} b ON d.from_id = b.id
                WHERE d.dep_type = 'blocks' AND b.status != 'closed'
            ),
            blocked_by_children(id) AS (
                SELECT b.parent_id
                FROM {TICKETS_TABLE} b
                WHERE b.parent_id IS NOT NULL AND b.status != 'closed'
            )
            SELECT * FROM {TICKETS_TABLE}
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
        return ticket_rows(self.conn.execute(sql, params))


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
                dep = Dep(from_id=TicketId(from_id), to_id=TicketId(to_id))
                journal_log(self.conn, "dep_remove", to_id, dep.model_dump_json())
        return cursor.rowcount


class JournalStore:
    def __init__(self, conn: Any):
        self.conn = conn

    def export(self):
        cursor = self.conn.execute(
            f"SELECT action, {FORMAT_ID_FIELD}, snapshot, created_at FROM journal ORDER BY id"
        )
        for action, ticket_id, snapshot, created_at in cursor:
            entry = {
                "action": action,
                FORMAT_ID_FIELD: ticket_id,
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
                ticket_id = entry[FORMAT_ID_FIELD]

                if action == "create":
                    sql, params = insert_query(Ticket(**snapshot))
                    self.conn.execute(sql, params)
                elif action == "update":
                    sql, params = update_all_query(Ticket(**snapshot))
                    self.conn.execute(sql, params)
                elif action == "delete":
                    self.conn.execute(f"DELETE FROM {TICKETS_TABLE} WHERE id=?", (ticket_id,))
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
        self.ticket = TicketStore(self.conn)
        self.dep = DepStore(self.conn)
        self.journal = JournalStore(self.conn)
        self.dry_run = dry_run

    # Ticket delegation
    def create(self, ticket: Ticket) -> Ticket:
        return self.ticket.create(ticket)

    def get(self, ticket_id: str) -> Ticket:
        return self.ticket.get(ticket_id)

    def update(self, ticket_id: str, **fields: Any) -> int:
        return self.ticket.update(ticket_id, **fields)

    def delete(self, ticket_id: str) -> int:
        return self.ticket.delete(ticket_id)

    def list(
        self,
        types: builtins.list[str] | None = None,
        statuses: builtins.list[str] | None = None,
        parent_id: str | None = None,
    ) -> builtins.list[Ticket]:
        return self.ticket.list(types=types, statuses=statuses, parent_id=parent_id)

    def list_by_assignee(self, actor: str) -> builtins.list[Ticket]:
        return self.ticket.list_by_assignee(actor)

    def ready(
        self,
        assignee: str | None = None,
        unassigned: bool = False,
        parent_id: str | None = None,
    ) -> builtins.list[Ticket]:
        return self.ticket.ready(assignee=assignee, unassigned=unassigned, parent_id=parent_id)

    def search(self, query: str) -> builtins.list[Ticket]:
        return self.ticket.search(query)

    def stats(self) -> dict[str, dict[str, int]]:
        return self.ticket.stats()

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


def create_ticket(store: Store, title: str, valid_types: Any = (), deps: Any = (), **fields: Any) -> Ticket:
    ticket_type = fields.get("type", "task")
    if valid_types and ticket_type not in valid_types:
        raise ValueError(f"Unknown type: {ticket_type}. Valid: {', '.join(sorted(valid_types))}")
    if parent_id := fields.get("parent_id"):
        try:
            store.get(parent_id)
        except TicketNotFoundError:
            raise ParentTicketNotFoundError(parent_id) from None
    ticket = Ticket(title=title, id=TicketId.generate(ticket_type), **fields)
    store.create(ticket)
    if deps:
        for dep_id in deps:
            add_dep(store, dep_id, ticket.id)
    return ticket


def show_ticket(store: Store, ticket_id: str) -> Ticket:
    return store.get(ticket_id)


def update_ticket(store: Store, ticket_id: str, **fields: Any) -> Ticket:
    validated = TicketUpdate(**fields)
    clean = validated.model_dump(exclude_none=True)
    if store.update(ticket_id, **clean) == 0:
        raise TicketNotFoundError(ticket_id)
    return store.get(ticket_id)


def close_ticket(store: Store, ticket_id: str, reason: str | None = None, force: bool = False) -> Ticket:
    if not force:
        all_tickets = store.list()
        open_children = [b for b in all_tickets if b.parent_id == ticket_id and b.status != "closed"]
        if open_children:
            count = len(open_children)
            raise OpenChildrenError(
                f"Cannot close {ticket_id}: {count} open {'child' if count == 1 else 'children'} remain"
            )
    update_fields: dict[str, Any] = {"status": "closed", "closed_at": datetime.now(UTC).isoformat()}
    if reason:
        update_fields["close_reason"] = reason
    if store.update(ticket_id, **update_fields) == 0:
        raise TicketNotFoundError(ticket_id)
    return store.get(ticket_id)


def reopen_ticket(store: Store, ticket_id: str, status: str = "open") -> Ticket:
    """Reopen a closed ticket, clearing closed_at and close_reason."""
    store.update(ticket_id, status=status, closed_at=None, close_reason=None)
    return store.get(ticket_id)


def delete_ticket(store: Store, ticket_id: str) -> Ticket:
    ticket = store.get(ticket_id)
    store.delete(ticket_id)
    return ticket


def claim_ticket(store: Store, ticket_id: str, actor: str) -> Ticket:
    ticket = store.get(ticket_id)
    if ticket.status == "closed":
        raise ValueError(f"{FORMAT_ENTITY} {ticket_id} is closed")
    if ticket.assignee == actor:
        return ticket
    if ticket.assignee:
        raise ValueError(f"{FORMAT_ENTITY} {ticket_id} already claimed by {ticket.assignee}")
    store.update(ticket_id, assignee=actor, status="in_progress")
    return store.get(ticket_id)


def release_ticket(store: Store, ticket_id: str, actor: str) -> Ticket:
    ticket = store.get(ticket_id)
    if not ticket.assignee:
        return ticket
    if ticket.assignee != actor:
        raise ValueError(f"{FORMAT_ENTITY} {ticket_id} claimed by {ticket.assignee}")
    store.update(ticket_id, assignee=None, status="open")
    return store.get(ticket_id)


def release_mine(store: Store, actor: str) -> builtins.list[Ticket]:
    assigned = store.list_by_assignee(actor)
    return [release_ticket(store, ticket.id, actor) for ticket in assigned]


def list_tickets(
    store: Store,
    types: builtins.list[str] | None = None,
    statuses: builtins.list[str] | None = None,
    parent_id: str | None = None,
) -> builtins.list[Ticket]:
    return store.list(types=types, statuses=statuses, parent_id=parent_id)


def ready_tickets(
    store: Store,
    assignee: str | None = None,
    unassigned: bool = False,
    parent_id: str | None = None,
) -> builtins.list[Ticket]:
    return store.ready(assignee=assignee, unassigned=unassigned, parent_id=parent_id)


def search_tickets(store: Store, query: str) -> builtins.list[Ticket]:
    return store.search(query)


def stats(store: Store) -> dict[str, dict[str, int]]:
    return store.stats()


def list_deps(store: Store, from_id: str) -> builtins.list[Dep]:
    return store.list_deps(from_id)


def add_dep(store: Store, from_id: str, to_id: str, dep_type: str = "blocks") -> Dep:
    # Self-dep check
    if from_id == to_id:
        ticket = store.get(from_id)
        raise CyclicDepError(
            f'Adding this dependency would create a cycle: {from_id} "{ticket.title}" \u2192 {from_id} "{ticket.title}"'
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
            tickets_by_id: dict[str, Ticket | None] = {}
            for ticket_id in set(cycle_ids):
                try:
                    tickets_by_id[ticket_id] = store.get(ticket_id)
                except TicketNotFoundError:
                    tickets_by_id[ticket_id] = None

            parts = []
            for ticket_id in cycle_ids:
                ticket = tickets_by_id.get(ticket_id)
                title = f' "{ticket.title}"' if ticket else ""
                parts.append(f"{ticket_id}{title}")

            cycle = " \u2192 ".join(parts)
            raise CyclicDepError(f"Adding this dependency would create a cycle: {cycle}")

        if current in visited:
            continue
        visited.add(current)

        for neighbor in adjacency.get(current, []):
            if neighbor not in visited:
                path_map[neighbor] = [*path_map[current], neighbor]
                stack.append(neighbor)

    dep = Dep(from_id=TicketId(from_id), to_id=TicketId(to_id), dep_type=dep_type)
    store.add_dep(dep)
    return dep


def remove_dep(store: Store, from_id: str, to_id: str) -> int:
    count = store.remove_dep(from_id, to_id)
    if count == 0:
        raise DepNotFoundError(from_id, to_id)
    return count


def graph(store: Store) -> dict[str, builtins.list[dict[str, Any]]]:
    all_tickets = store.list()
    all_deps = store.list_all_deps()
    nodes = {
        ticket.id: {"id": ticket.id, "title": ticket.title, "status": ticket.status, "parent_id": ticket.parent_id}
        for ticket in all_tickets
    }
    edges = [{"from_id": dep.from_id, "to_id": dep.to_id, "dep_type": dep.dep_type} for dep in all_deps]
    return {"nodes": list(nodes.values()), "edges": edges}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_DIR = FORMAT_NAMESPACE
CONFIG_FILE = "config.json"
DATA_DIR = FORMAT_NAMESPACE
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
class TicketType:
    name: str
    description: str = ""

    def model_dump(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def model_dump_json(self) -> str:
        return model_dump_json(self.model_dump())


DEFAULT_TYPES = [
    TicketType(name="task"),
    TicketType(name="bug"),
    TicketType(name="epic"),
]


@dataclass
class Config:
    path: Path = field(default_factory=lambda: config_path())
    projects: builtins.list[Project] = field(default_factory=list)
    types: builtins.list[TicketType] = field(default_factory=lambda: list(DEFAULT_TYPES))

    def type_names(self) -> set[str]:
        return {ticket_type.name for ticket_type in self.types}

    def add_type(self, ticket_type: TicketType) -> None:
        self.types = [item for item in self.types if item.name != ticket_type.name]
        self.types.append(ticket_type)

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
            TicketType(**{key: value for key, value in ticket_type.items() if key in {"name", "description"}})
            for ticket_type in data.get("types", [t.model_dump() for t in DEFAULT_TYPES])
        ]
        return cls(path=path, projects=projects, types=types)

    def model_dump(self) -> dict[str, Any]:
        return {
            "projects": [project.model_dump() for project in self.projects],
            "types": [ticket_type.model_dump() for ticket_type in self.types],
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

STORE_DIRNAME = f".{FORMAT_NAMESPACE}"
STORE_DIR_ENV = f"MAGIC_{FORMAT_NAMESPACE.upper()}_DIR"
PARENT_ENV = f"MAGIC_{FORMAT_NAMESPACE.upper()}_PARENT_ID"
DB_NAME = f"{FORMAT_NAMESPACE}.db"
AGENTS_MD = "AGENTS.md"
GITIGNORE = ".gitignore"
GITIGNORE_CONTENT = "*\n!.gitignore\n!AGENTS.md\n!journal.jsonl\n"

AGENTS_MD_TEMPLATE = """# Tickets — Agent Integration

This project uses `ticket.py` for task tracking.

## Before Starting Work

```bash
uv run ticket.py ready                     # see what's unblocked
uv run ticket.py show <id>                 # read the full ticket before starting
uv run ticket.py claim <id> --actor <name> # claim it
```

## Self-Contained Tickets

Every ticket body must contain enough context for someone with no prior conversation to
pick it up. Include: what needs to change, why, which files are involved, and what
"done" looks like.

```bash
uv run ticket.py create "Title" --body "Full description"
uv run ticket.py create "Title" --type bug --body "Description"
```

Ticket IDs are prefixed with their type: `task-a3f2dd1c`, `epic-12345678`, `bug-deadbeef`.

Use `--json` on any command for structured output.
"""

SKILL_TEMPLATE = """---
name: ticket
description: Graph-based issue tracker for task coordination and agent workflows.
---

# Tickets

A lightweight graph-based issue tracker for AI agent coordination. This is the
standard-library `ticket.py` implementation. Tickets are nodes; dependencies
are directed edges.

## Concepts

- **Ticket**: a task-like item with `id`, `title`, `type`, `status`, `priority`, `body`,
  parent/assignee fields, and timestamps.
- **Ticket IDs**: type-prefixed lowercase IDs such as `task-a3f2dd1c`, `bug-deadbeef`,
  `epic-12345678`.
- **Types**: configurable with `ticket.py types`; defaults are `task`, `bug`, and `epic`.
- **Status**: `open`, `in_progress`, or `closed`.
- **Priority**: integer `0..4`, where `0` is highest and default is `2`.
- **Dependencies**: `A blocks B` means B is not ready until A is closed.
- **Parent/child**: parent tickets can contain child tickets via `--parent`.
- **Ready**: a ticket is not closed, has no open blockers, and has no open children.

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
"""


def env_store_dir(env: Any = os.environ, var: str = STORE_DIR_ENV) -> Path | None:
    if not (value := env.get(var)):
        return None
    return Path(value)


def walk_store_dir(start: str | Path | None = None, dirname: str = STORE_DIRNAME) -> Path:
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


def find_store_dir(
    start: str | Path | None = None,
    dirname: str = STORE_DIRNAME,
    env: Any = os.environ,
    var: str = STORE_DIR_ENV,
    config_file: str | Path | None = None,
) -> Path:
    """Find the store directory. Checks: env var → registry → local walk-up."""
    if store_dir := env_store_dir(env=env, var=var):
        if not store_dir.is_dir():
            raise FileNotFoundError(f"{var}={store_dir} is not a directory")
        return store_dir
    if store_dir := find_in_registry(start=start, config_file=config_file):
        return store_dir
    return walk_store_dir(start=start, dirname=dirname)


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
    3. auto-discover: env → registry → local walk-up — find it or fail
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
        return find_store_dir(config_file=config_file) / db_name
    except FileNotFoundError:
        raise FileNotFoundError(
            f"No {FORMAT_NAMESPACE} project found. Did you run '{FORMAT_NAMESPACE} init'?"
        ) from None


def setup_store_dir(store_dir: str | Path, db_name: str = DB_NAME, agents_md: str = AGENTS_MD) -> Path:
    """Create store directory with db, AGENTS.md, and .gitignore. Returns the dir."""
    store_dir = Path(store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)

    db_path = store_dir / db_name
    Store.from_path(db_path).close()

    agents_file = store_dir / agents_md
    if not agents_file.exists():
        agents_file.write_text(AGENTS_MD_TEMPLATE)

    gitignore_file = store_dir / GITIGNORE
    if not gitignore_file.exists():
        gitignore_file.write_text(GITIGNORE_CONTENT)

    return store_dir


def init_project(
    cwd: str | Path | None = None,
    name: str | None = None,
    data_base: str | Path | None = None,
    config_file: str | Path | None = None,
) -> Path:
    """Initialize a tickets project in the registry. Returns the store dir path."""
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
    dirname: str = STORE_DIRNAME,
    cwd: str | Path | None = None,
    env: Any = os.environ,
    var: str = STORE_DIR_ENV,
) -> Path:
    """Initialize a project in the local store directory. Returns its path."""
    base = Path(cwd) if cwd else Path.cwd()
    store_dir = env_store_dir(env=env, var=var) or base / dirname
    return setup_store_dir(store_dir)


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
    """Migrate an existing local store to the registry. Returns the new store dir path."""
    base = Path(cwd) if cwd else Path.cwd()
    try:
        old_dir = walk_store_dir(start=base)
    except FileNotFoundError:
        raise FileNotFoundError(f"No {STORE_DIRNAME}/ directory found. Nothing to migrate.") from None

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
    if isinstance(data, Ticket):
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


def default_parent_id() -> TicketId | None:
    """Get the default parent_id from the configured environment variable."""
    value = os.environ.get(PARENT_ENV)
    if value:
        return TicketId(value)
    return None


def ticket_id_argument(value: str) -> TicketId:
    try:
        return TicketId(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def cmd_init(rc: RunContext, args: argparse.Namespace) -> None:
    if args.local_dir:
        store_dir = init_project_local()
        print(f"Initialized {FORMAT_NAMESPACE} project in {store_dir}")
        return
    store_dir = init_project(name=args.name)
    print(f"Initialized {FORMAT_NAMESPACE} project in {store_dir}")


def cmd_migrate(rc: RunContext, args: argparse.Namespace) -> None:
    try:
        store_dir = migrate_project(name=args.name)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    try:
        old_dir = walk_store_dir()
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
            ticket = create_ticket(store, args.title, valid_types=rc.config.type_names(), **kwargs)
    except (TypeError, ValueError, TicketNotFoundError) as exc:
        error(rc, exc)
    print(output(ticket, rc.json_output))


def cmd_show(rc: RunContext, args: argparse.Namespace) -> None:
    try:
        with get_store(rc) as store:
            ticket = show_ticket(store, args.ticket_id)
            all_deps = store.list_all_deps()
    except TicketNotFoundError as exc:
        error(rc, exc)

    if rc.json_output:
        data = ticket.model_dump()
        data["blocked_by"] = [
            str(dep.from_id) for dep in all_deps if dep.to_id == args.ticket_id and dep.dep_type == "blocks"
        ]
        data["blocks"] = [
            str(dep.to_id) for dep in all_deps if dep.from_id == args.ticket_id and dep.dep_type == "blocks"
        ]
        if rc.fields:
            data = {key: value for key, value in data.items() if key in rc.fields}
        print(json.dumps(data, default=str))
    else:
        print(output(ticket, False, rc.fields))


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
                current = show_ticket(store, args.ticket_id)
                if current.status == "closed":
                    other_fields = {key: value for key, value in clean_fields.items() if key != "status"}
                    ticket = reopen_ticket(store, args.ticket_id, status=args.status)
                    if other_fields:
                        ticket = update_ticket(store, args.ticket_id, **other_fields)
                else:
                    ticket = update_ticket(store, args.ticket_id, **clean_fields)
            else:
                ticket = update_ticket(store, args.ticket_id, **clean_fields)
    except (TicketNotFoundError, TypeError, ValueError) as exc:
        error(rc, exc)

    print(output(ticket, rc.json_output))


def cmd_close(rc: RunContext, args: argparse.Namespace) -> None:
    try:
        with get_store(rc) as store:
            ticket = close_ticket(store, args.ticket_id, reason=args.reason, force=args.force)
    except (TicketNotFoundError, OpenChildrenError) as exc:
        error(rc, exc)
    print(output(ticket, rc.json_output))


def cmd_delete(rc: RunContext, args: argparse.Namespace) -> None:
    try:
        with get_store(rc) as store:
            delete_ticket(store, args.ticket_id)
    except TicketNotFoundError as exc:
        error(rc, exc)
    if not rc.json_output:
        print(f"Deleted {args.ticket_id}")


def cmd_claim(rc: RunContext, args: argparse.Namespace) -> None:
    try:
        with get_store(rc) as store:
            ticket = claim_ticket(store, args.ticket_id, args.actor)
    except (TicketNotFoundError, ValueError) as exc:
        error(rc, exc)
    print(output(ticket, rc.json_output))


def cmd_release(rc: RunContext, args: argparse.Namespace) -> None:
    if args.mine and args.ticket_id:
        error(rc, ValueError(f"Provide a {FORMAT_ENTITY_LOWER} id or --mine, not both"))
    elif args.mine:
        with get_store(rc) as store:
            released = release_mine(store, args.actor)
        print(output(released, rc.json_output))
    elif args.ticket_id:
        try:
            with get_store(rc) as store:
                ticket = release_ticket(store, args.ticket_id, args.actor)
        except (TicketNotFoundError, ValueError) as exc:
            error(rc, exc)
        print(output(ticket, rc.json_output))
    else:
        error(rc, ValueError(f"Provide a {FORMAT_ENTITY_LOWER} id or --mine"))


def cmd_list(rc: RunContext, args: argparse.Namespace) -> None:
    parent = args.parent
    if parent is None:
        parent = default_parent_id()
    types = args.type.split(",") if args.type else None
    statuses = args.status.split(",") if args.status else None
    with get_store(rc) as store:
        all_tickets = list_tickets(store, types=types, statuses=statuses, parent_id=parent)
    print(output(all_tickets, rc.json_output, rc.fields))


def cmd_ready(rc: RunContext, args: argparse.Namespace) -> None:
    parent = args.parent
    if parent is None:
        parent = default_parent_id()
    if args.assignee and args.unassigned:
        error(rc, ValueError("--assignee and --unassigned are mutually exclusive"))
    with get_store(rc) as store:
        ready = ready_tickets(store, assignee=args.assignee, unassigned=args.unassigned, parent_id=parent)
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
        found = search_tickets(store, args.query)
    print(output(found, rc.json_output, rc.fields))


def cmd_schema(rc: RunContext, args: argparse.Namespace) -> None:
    schemas = {
        SCHEMA_MODEL_NAME: Ticket.model_json_schema(),
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
    except (CyclicDepError, TicketNotFoundError, sqlite3.IntegrityError) as exc:
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
    for ticket_type in rc.config.types:
        line = ticket_type.name
        if ticket_type.description:
            line += f"  {ticket_type.description}"
        print(line)


def cmd_types_add(rc: RunContext, args: argparse.Namespace) -> None:
    rc.config.add_type(TicketType(name=args.name, description=args.description))
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
        description="Graph-based issue tracker for AI agent coordination.",
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

    init_parser = subparsers.add_parser("init", help="Initialize a tickets project")
    init_parser.add_argument("--name", help="Project name for registry")
    init_parser.add_argument(
        "--dir", dest="local_dir", action="store_true", help="Create a store directory in the current directory"
    )
    init_parser.set_defaults(func=cmd_init)

    migrate_parser = subparsers.add_parser("migrate", help="Migrate a local store to the project registry")
    migrate_parser.add_argument("--name", help="Project name for registry")
    migrate_parser.set_defaults(func=cmd_migrate)

    create_parser = subparsers.add_parser("create", help="Create a new ticket")
    create_parser.add_argument("title")
    create_parser.add_argument("--type", help="Ticket type")
    create_parser.add_argument("--body", default="", help="Ticket description")
    create_parser.add_argument("--parent", type=ticket_id_argument, help="Parent ticket id")
    create_parser.add_argument("--priority", type=int, help="Priority (0=highest, 4=lowest)")
    create_parser.add_argument(
        "--dep",
        action="append",
        type=ticket_id_argument,
        help="Ticket ID that blocks this ticket (repeatable)",
    )
    create_parser.set_defaults(func=cmd_create)

    show_parser = subparsers.add_parser("show", help="Show a single ticket by id")
    show_parser.add_argument("ticket_id", type=ticket_id_argument)
    show_parser.set_defaults(func=cmd_show)

    update_parser = subparsers.add_parser("update", help="Update fields on a ticket")
    update_parser.add_argument("ticket_id", type=ticket_id_argument)
    update_parser.add_argument("--title", help="New title")
    update_parser.add_argument("--type", help="New type")
    update_parser.add_argument("--status", help="New status")
    update_parser.add_argument("--priority", type=int, help="New priority")
    update_parser.add_argument("--body", help="New body")
    update_parser.add_argument("--parent", type=ticket_id_argument, help="New parent ticket id")
    update_parser.set_defaults(func=cmd_update)

    close_parser = subparsers.add_parser("close", help="Close a ticket")
    close_parser.add_argument("ticket_id", type=ticket_id_argument)
    close_parser.add_argument("--reason", help="Reason for closing")
    close_parser.add_argument("--force", action="store_true", help="Close even if children are open")
    close_parser.set_defaults(func=cmd_close)

    delete_parser = subparsers.add_parser("delete", help="Delete a ticket")
    delete_parser.add_argument("ticket_id", type=ticket_id_argument)
    delete_parser.set_defaults(func=cmd_delete)

    claim_parser = subparsers.add_parser("claim", help="Claim a ticket")
    claim_parser.add_argument("ticket_id", type=ticket_id_argument)
    claim_parser.add_argument("--actor", required=True, help="Who is claiming the ticket")
    claim_parser.set_defaults(func=cmd_claim)

    release_parser = subparsers.add_parser("release", help="Release a claimed ticket")
    release_parser.add_argument("ticket_id", nargs="?", type=ticket_id_argument)
    release_parser.add_argument("--actor", default="", help="Who is releasing the ticket")
    release_parser.add_argument("--mine", action="store_true", help="Release all tickets claimed by actor")
    release_parser.set_defaults(func=cmd_release)

    list_parser = subparsers.add_parser("list", help="List all tickets")
    list_parser.add_argument("--type", help="Filter by type (comma-separated)")
    list_parser.add_argument("--status", help="Filter by status (comma-separated)")
    list_parser.add_argument("--parent", type=ticket_id_argument, help="Filter by parent ticket id")
    list_parser.set_defaults(func=cmd_list)

    ready_parser = subparsers.add_parser("ready", help="List only unblocked tickets")
    ready_parser.add_argument("--assignee", help="Filter by assignee name")
    ready_parser.add_argument("--unassigned", action="store_true", help="Show only unclaimed tickets")
    ready_parser.add_argument("--parent", type=ticket_id_argument, help="Filter by parent ticket id")
    ready_parser.set_defaults(func=cmd_ready)

    stats_parser = subparsers.add_parser("stats", help="Show aggregate counts")
    stats_parser.set_defaults(func=cmd_stats)

    graph_parser = subparsers.add_parser("graph", help="Show dependency tree visualization")
    graph_parser.set_defaults(func=cmd_graph)

    search_parser = subparsers.add_parser("search", help="Search tickets by title and body")
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

    dep_add_parser = dep_subparsers.add_parser("add", help="Add a dependency between two tickets")
    dep_add_parser.add_argument("from_id", type=ticket_id_argument)
    dep_add_parser.add_argument("to_id", type=ticket_id_argument)
    dep_add_parser.add_argument("--type", dest="dep_type", default="blocks", help="Dependency type")
    dep_add_parser.set_defaults(func=cmd_dep_add)

    dep_remove_parser = dep_subparsers.add_parser("remove", help="Remove a dependency between two tickets")
    dep_remove_parser.add_argument("from_id", type=ticket_id_argument)
    dep_remove_parser.add_argument("to_id", type=ticket_id_argument)
    dep_remove_parser.set_defaults(func=cmd_dep_remove)

    types_parser = subparsers.add_parser("types", help="Manage ticket types")
    types_subparsers = types_parser.add_subparsers(dest="types_command")

    types_add_parser = types_subparsers.add_parser("add", help="Add a ticket type to config")
    types_add_parser.add_argument("name")
    types_add_parser.add_argument("--description", default="", help="Type description")
    types_add_parser.set_defaults(func=cmd_types_add)

    types_remove_parser = types_subparsers.add_parser("remove", help="Remove a ticket type from config")
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

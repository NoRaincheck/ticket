# ticket.py

A graph-based issue tracker in a single Python file. `ticket.py` uses only the
standard library and stores everything in SQLite — no installation, package
manager, or third-party dependency required.

Copy `ticket.py` anywhere and run it directly with Python 3.12+:

```bash
uv run ticket.py --help
# or
python3 ticket.py --help
```

## Quick start

```bash
# Create a local .beans/ store in the current directory
uv run ticket.py init --dir

# Create and inspect beans
uv run ticket.py create "Fix auth middleware" --body "Tokens expire too early"
uv run ticket.py list
uv run ticket.py show task-a3f2dd1c

# Workflow
uv run ticket.py claim task-a3f2dd1c --actor alice
uv run ticket.py ready
uv run ticket.py close task-a3f2dd1c --reason "Fixed in abc1234"
```

Bean IDs are generated automatically and prefixed with the bean type, for
example `task-a3f2dd1c`, `bug-deadbeef`, or `epic-12345678`.

## Concepts

### Beans

A bean is a task-like item with:

| Field | Description |
| --- | --- |
| `id` | Type-prefixed identifier such as `task-a3f2dd1c` |
| `title` | Short summary (required) |
| `type` | Configurable type; defaults to `task` |
| `status` | `open`, `in_progress`, or `closed` |
| `priority` | Integer `0`–`4`; `0` is highest, default is `2` |
| `body` | Free-form details |
| `parent_id` | Optional parent bean |
| `assignee` | Set when a bean is claimed |
| `created_by`, `ref_id` | Optional external references |
| `created_at`, `closed_at`, `close_reason` | Timestamps and close metadata |

### Status and priority

- `open` — available to start.
- `in_progress` — claimed or actively being worked on.
- `closed` — done; hidden from `ready`.
- Priority is `0` (highest) through `4` (lowest).

### Dependencies

`dep add A B` means **A blocks B**: B is not ready until A is closed.
Dependencies are directed edges in the bean graph.

- Closing a blocker makes its dependents ready.
- Adding a dependency that would create a cycle is rejected.
- `show --json` includes `blocked_by` and `blocks` lists for a bean.

### Parents and readiness

Create hierarchies with `create "Child" --parent <epic-id>`. A bean is
**ready** when it is not closed, has no open blocker (direct or transitive),
and has no open children. Use `ready` to see only beans that can be started.

### Types

Types are stored in the config file. The defaults are `task`, `bug`, and
`epic`. Manage them with `types add` and `types remove`; creating a bean with an
unregistered type fails.

## Commands

### Global options

These options go before the subcommand:

| Option | Description |
| --- | --- |
| `--db PATH` | Use a specific SQLite database |
| `--project NAME` | Use a project registered in the config |
| `--json` | Emit machine-readable JSON |
| `--dry-run` | Perform writes in a transaction that is rolled back |
| `--fields LIST` | With `--json`, include only the comma-separated fields |
| `--version` | Print the version |

### Setup and configuration

```bash
ticket.py init --dir                 # create .beans/ in the current directory
ticket.py init --name myproject      # register a project in the user data dir
ticket.py migrate                    # move .beans/ into the project registry
ticket.py config                     # show config path and contents
```

`init --dir` creates the SQLite database plus `.gitignore` and `AGENTS.md`
inside `.beans/`. `init` without `--dir` registers the current project using
its Git remote (or absolute path) and stores data under the user data
directory. `migrate` copies an existing `.beans/` store into that registry and
offers to delete the old directory.

### Creating and reading

```bash
ticket.py create "Title" [--type TYPE] [--body TEXT]
                        [--parent ID] [--priority 0-4] [--dep BLOCKER_ID ...]
ticket.py show ID
ticket.py list [--type T[,T]] [--status S[,S]] [--parent ID]
ticket.py ready [--assignee NAME | --unassigned] [--parent ID]
ticket.py search QUERY
ticket.py stats
ticket.py graph
```

- `create --dep` attaches blockers to the new bean; repeat `--dep` as needed.
- `list` and `ready` also honor `MAGIC_BEANS_PARENT_ID` when `--parent` is
  omitted.
- `ready --assignee` and `ready --unassigned` are mutually exclusive.
- `stats` reports counts by status, type, and assignee.
- `graph` prints a tree with parent/child nesting and blocker annotations.

### Updating and workflow

```bash
ticket.py update ID [--title TEXT] [--type TYPE] [--status STATUS]
                    [--priority 0-4] [--body TEXT] [--parent ID]
ticket.py close ID [--reason TEXT] [--force]
ticket.py delete ID
ticket.py claim ID --actor NAME
ticket.py release [ID] --actor NAME
ticket.py release --mine --actor NAME
```

- Updating a closed bean to `open` or `in_progress` reopens it and clears
  `closed_at` and `close_reason`.
- `close` refuses to close a bean with open children unless `--force` is used.
- `claim` sets the assignee and moves the bean to `in_progress`. A bean already
  claimed by someone else cannot be claimed again.
- `release` clears the assignee and returns the bean to `open`; `--mine`
  releases every bean claimed by the actor.

### Dependencies

```bash
ticket.py dep add FROM_ID TO_ID [--type blocks]
ticket.py dep remove FROM_ID TO_ID
```

`FROM_ID` blocks `TO_ID`. Cycle detection spans the existing dependency graph,
including self-dependencies.

### Types

```bash
ticket.py types
ticket.py types add NAME [--description TEXT]
ticket.py types remove NAME
```

### Schemas and journals

```bash
ticket.py schema                     # JSON schemas for Bean, Dep, and Error
ticket.py skill                      # agent-integration skill text
ticket.py export-journal             # one JSON object per line
ticket.py --db rebuilt.db rebuild journal.jsonl
```

Every create, update, delete, and dependency change is recorded in an internal
journal. `export-journal` emits JSONL; `rebuild` replays that JSONL into a
database (specified with `--db`, or discovered like any other command).

## Output formats

Without `--json`, beans print as one line per bean:

```text
task-a3f2dd1c  2026-11-05 14:30  Fix auth middleware
```

Use `show --json` to see the full bean, including its body. With `--json`,
beans, dependencies, and errors are compact JSON objects. `show --json` also
includes dependency relationships:

```json
{"id":"task-a3f2dd1c","title":"Fix auth middleware",...,"blocked_by":[],"blocks":["task-12345678"]}
```

`schema` and `export-journal` always emit JSON, while `stats` and `graph` have
both human-readable and `--json` forms. `--fields` narrows JSON objects to the
requested keys.

Errors exit with status `1`. Text errors go to stderr; with `--json`, errors
print as `{"message": "..."}` on stdout.

## Project discovery

Commands look for a store in this order:

1. `--db PATH`, if provided.
2. `--project NAME`, looked up in the registry.
3. `MAGIC_BEANS_DIR`, if set.
4. A matching project in the registry, identified by the current Git remote or
   directory path.
5. A `.beans/` directory in the current directory or any parent.

If none is found, the command fails and suggests running `init`.

### Environment variables

| Variable | Purpose |
| --- | --- |
| `MAGIC_BEANS_DIR` | Store directory to use instead of discovery |
| `MAGIC_BEANS_PARENT_ID` | Default parent for `create`, `list`, and `ready` |

## Configuration and data locations

Configuration and registered project data follow XDG conventions:

| Path | Contents |
| --- | --- |
| `$XDG_CONFIG_HOME/beans/config.json` | Registered projects and bean types |
| `$XDG_DATA_HOME/beans/<project>/` | Registry store with `beans.db`, `AGENTS.md`, `.gitignore` |
| `.beans/` | Local store created by `init --dir` |

`XDG_CONFIG_HOME` defaults to `~/.config` and `XDG_DATA_HOME` to
`~/.local/share`. Each store contains `beans.db`, an `AGENTS.md` describing
agent conventions, and a `.gitignore` that ignores store contents except
`.gitignore`, `AGENTS.md`, and `journal.jsonl`.

## Dry runs

`--dry-run` wraps mutations in a transaction that is rolled back when the
command exits, so you can preview `create`, `update`, `close`, `delete`,
`claim`, `release`, `dep add/remove`, and `rebuild` output without changing the
database.

```bash
uv run ticket.py --dry-run create "Try this"
uv run ticket.py --json --dry-run close task-a3f2dd1c
```

## Python API

The same functionality is importable without the CLI:

```python
from pathlib import Path

import ticket

with ticket.Store.from_path(Path("issues.db")) as store:
    bean = ticket.create_bean(store, "Fix auth", body="Tokens expire too early")
    blocker = ticket.create_bean(store, "Rotate keys", type="task")
    ticket.add_dep(store, blocker.id, bean.id)
    ticket.claim_bean(store, blocker.id, "alice")

    for ready in ticket.ready_beans(store):
        print(ready.id, ready.title)
```

Useful entry points include `Store`, `Bean`, `Dep`, `Config`, `create_bean`,
`show_bean`, `update_bean`, `close_bean`, `reopen_bean`, `delete_bean`,
`claim_bean`, `release_bean`, `list_beans`, `ready_beans`, `search_beans`,
`stats`, `add_dep`, `remove_dep`, `list_deps`, and `graph`.

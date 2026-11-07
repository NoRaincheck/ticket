from __future__ import annotations

import ast
import json

from conftest import TICKET


def test_init_dir_creates_ticket_store(initialized_cli):
    cli = initialized_cli
    store = cli.cwd / ".ticket"
    assert (store / "ticket.db").exists()
    assert (store / "AGENTS.md").exists()
    assert (store / ".gitignore").read_text() == "*\n!.gitignore\n!AGENTS.md\n!journal.jsonl\n"


def test_crud_workflow(initialized_cli):
    cli = initialized_cli
    code, task = cli.json("--json", "create", "Fix auth", "--body", "Tokens expire early")
    assert code == 0
    assert task["id"].startswith("task-")
    assert task["body"] == "Tokens expire early"

    code, bug = cli.json("--json", "create", "Add tests", "--type", "bug", "--priority", "0")
    assert code == 0
    assert bug["type"] == "bug"

    code, listed = cli.json("--json", "list")
    assert code == 0
    assert {item["id"] for item in listed} == {task["id"], bug["id"]}

    code, updated = cli.json("--json", "update", task["id"], "--title", "Fix auth middleware")
    assert code == 0
    assert updated["title"] == "Fix auth middleware"

    code, claimed = cli.json("--json", "claim", task["id"], "--actor", "alice")
    assert code == 0
    assert claimed["assignee"] == "alice"
    assert claimed["status"] == "in_progress"

    code, released = cli.json("--json", "release", task["id"], "--actor", "alice")
    assert code == 0
    assert released["assignee"] is None
    assert released["status"] == "open"

    code, _ = cli.json("--json", "dep", "add", task["id"], bug["id"])
    assert code == 0
    code, blocked = cli.json("--json", "show", bug["id"])
    assert code == 0
    assert blocked["blocked_by"] == [task["id"]]

    code, ready = cli.json("--json", "ready")
    assert code == 0
    assert [item["id"] for item in ready] == [task["id"]]

    code, closed = cli.json("--json", "close", task["id"], "--reason", "Fixed in abc1234")
    assert code == 0
    assert closed["status"] == "closed"
    assert closed["close_reason"] == "Fixed in abc1234"

    code, ready = cli.json("--json", "ready")
    assert code == 0
    assert [item["id"] for item in ready] == [bug["id"]]

    deleted = cli.run("--json", "delete", bug["id"])
    assert deleted.returncode == 0
    assert deleted.stdout == ""

    code, error = cli.json("--json", "show", bug["id"])
    assert code == 1
    assert error["message"] == f"Ticket not found: {bug['id']}"


def test_errors_and_validation(initialized_cli):
    cli = initialized_cli
    code, error = cli.json("--json", "create", "Bad", "--priority", "5")
    assert code == 1
    assert "priority" in error["message"].lower()

    code, error = cli.json("--json", "create", "Bad", "--type", "missing")
    assert code == 1
    assert "Unknown type" in error["message"]

    code, first = cli.json("--json", "create", "First")
    code, second = cli.json("--json", "create", "Second")
    code, _ = cli.json("--json", "dep", "add", first["id"], second["id"])
    assert code == 0

    code, error = cli.json("--json", "dep", "add", second["id"], first["id"])
    assert code == 1
    assert "cycle" in error["message"].lower()

    code, error = cli.json("--json", "ready", "--assignee", "alice", "--unassigned")
    assert code == 1
    assert "mutually exclusive" in error["message"]


def test_parent_children_and_env_parent(initialized_cli):
    cli = initialized_cli
    code, parent = cli.json("--json", "create", "Epic", "--type", "epic")
    assert code == 0

    cli.env["TICKET_PARENT_ID"] = parent["id"]
    code, scoped = cli.json("--json", "create", "Scoped")
    assert code == 0
    assert scoped["parent_id"] == parent["id"]

    code, listed = cli.json("--json", "list")
    assert code == 0
    assert [item["id"] for item in listed] == [scoped["id"]]

    child = cli.json("--json", "create", "Child")[1]
    code, error = cli.json("--json", "close", parent["id"])
    assert code == 1
    assert "open child" in error["message"]

    code, forced = cli.json("--json", "close", parent["id"], "--force")
    assert code == 0
    assert forced["status"] == "closed"

    del cli.env["TICKET_PARENT_ID"]
    code, everything = cli.json("--json", "list")
    assert code == 0
    assert {item["id"] for item in everything} == {parent["id"], scoped["id"], child["id"]}


def test_filters_fields_and_search(initialized_cli):
    cli = initialized_cli
    code, bug = cli.json("--json", "create", "Fix auth", "--type", "bug", "--body", "authentication")
    assert code == 0
    code, task = cli.json("--json", "create", "Add tests")
    assert code == 0

    code, filtered = cli.json("--json", "--fields", "id,title", "list")
    assert code == 0
    assert all(set(item) == {"id", "title"} for item in filtered)

    code, found = cli.json("--json", "search", "auth")
    assert code == 0
    assert [item["id"] for item in found] == [bug["id"]]

    code, stats = cli.json("--json", "stats")
    assert code == 0
    assert stats["by_type"] == {"bug": 1, "task": 1}

    code, graph = cli.json("--json", "graph")
    assert code == 0
    assert {node["id"] for node in graph["nodes"]} == {bug["id"], task["id"]}


def test_schema_uses_ticket_vocabulary(initialized_cli):
    cli = initialized_cli
    code, schema = cli.json("--json", "schema")
    assert code == 0
    assert set(schema) == {"Ticket", "Dep", "Error"}
    assert schema["Ticket"]["title"] == "Ticket"


def test_config_types_and_registry(cli):
    result = cli.run("--json", "init", "--name", "myblog")
    assert result.returncode == 0

    config_file = cli.root / "config" / "ticket" / "config.json"
    store_dir = cli.root / "data" / "ticket" / "myblog"
    assert config_file.exists()
    assert (store_dir / "ticket.db").exists()
    assert not (cli.cwd / ".ticket").exists()

    result = cli.run("config")
    assert result.returncode == 0
    assert str(config_file) in result.stdout

    result = cli.run("types", "add", "spike", "--description", "Investigation")
    assert result.returncode == 0
    code, created = cli.json("--json", "create", "Spike", "--type", "spike")
    assert code == 0
    assert created["type"] == "spike"

    assert cli.run("types", "remove", "spike").returncode == 0
    code, error = cli.json("--json", "create", "Removed", "--type", "spike")
    assert code == 1
    assert "Unknown type" in error["message"]


def test_store_dir_env_override(cli):
    store_dir = cli.root / "custom-store"
    cli.env["TICKET_DIR"] = str(store_dir)
    assert cli.run("init", "--dir").returncode == 0
    assert (store_dir / "ticket.db").exists()
    assert not (cli.cwd / ".ticket").exists()

    code, created = cli.json("--json", "create", "Override")
    assert code == 0
    assert created["id"].startswith("task-")


def test_journal_export_and_rebuild(initialized_cli):
    cli = initialized_cli
    code, ticket = cli.json("--json", "create", "Fix auth")
    assert code == 0
    assert cli.json("--json", "update", ticket["id"], "--title", "Updated")[0] == 0
    assert cli.json("--json", "close", ticket["id"])[0] == 0

    exported = cli.run("export-journal")
    assert exported.returncode == 0
    entries = [json.loads(line) for line in exported.stdout.splitlines()]
    assert [entry["action"] for entry in entries] == ["create", "update", "update"]
    assert all(entry["ticket_id"] == ticket["id"] for entry in entries)

    journal = cli.root / "journal.jsonl"
    journal.write_text(exported.stdout)
    rebuilt = cli.root / "rebuilt.db"
    result = cli.run("--db", str(rebuilt), "rebuild", str(journal))
    assert result.returncode == 0

    code, tickets = cli.json("--db", str(rebuilt), "--json", "list")
    assert code == 0
    assert [item["id"] for item in tickets] == [ticket["id"]]
    assert tickets[0]["title"] == "Updated"


def test_help_lists_commands(cli):
    result = cli.run("--help")
    assert result.returncode == 0
    for command in {
        "init",
        "create",
        "show",
        "update",
        "close",
        "delete",
        "claim",
        "release",
        "list",
        "ready",
        "stats",
        "graph",
        "search",
        "schema",
        "config",
        "skill",
        "export-journal",
        "rebuild",
        "dep",
        "types",
    }:
        assert command in result.stdout


def test_source_is_stdlib_only_and_uses_ticket_vocabulary():
    source = TICKET.read_text()
    tree = ast.parse(source)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    stdlib = {
        "__future__",
        "argparse",
        "builtins",
        "dataclasses",
        "datetime",
        "functools",
        "json",
        "os",
        "pathlib",
        "re",
        "secrets",
        "shutil",
        "sqlite3",
        "subprocess",
        "sys",
        "typing",
    }
    assert imports <= stdlib
    assert 'FORMAT_NAMESPACE = "ticket"' in source
    assert 'TICKETS_TABLE = "tickets"' in source
    assert 'FORMAT_ID_FIELD = f"{FORMAT_ENTITY_LOWER}_id"' in source

from __future__ import annotations

import ast
import json

from conftest import ID_RE, TICKET, TIMESTAMP_RE, normalize


def capture_json(session, log, label, *args):
    code, data = session.json(*args)
    log.append((label, code, session.normalized(data)))
    return data


def capture_text(session, log, label, *args, input=None):
    result = session.run(*args, input=input)
    log.append((label, result.returncode, session.normalized(result.stdout.strip())))
    return result


def capture_error(session, log, label, *args):
    result = session.run(*args)
    output = result.stdout.strip()
    try:
        data = json.loads(output) if output else None
    except json.JSONDecodeError:
        data = output or result.stderr.strip()
    if isinstance(data, dict) and "message" in data:
        # magic-beans 0.7.0 wraps KeyError messages in quotes and predates the
        # instructive "Bean not found" wording added on main. Normalize that
        # one known upstream difference while still checking the error class.
        message = data["message"].strip("'")
        if ID_RE.fullmatch(message):
            message = f"Bean not found: {message}"
        lowered = message.lower()
        if "priority" in lowered and ("validation error" in lowered or "priority must be" in lowered):
            message = "<validation: priority>"
        elif "status" in lowered and ("validation error" in lowered or "invalid status" in lowered):
            message = "<validation: status>"
        data["message"] = message
    log.append((label, result.returncode, session.normalized(data)))
    return result


def crud_scenario(session):
    log = []
    first = capture_json(session, log, "create task", "--json", "create", "Fix auth")
    second = capture_json(
        session,
        log,
        "create bug",
        "--json",
        "create",
        "Add tests",
        "--type",
        "bug",
        "--priority",
        "0",
        "--body",
        "Cover the auth flow",
    )
    capture_json(session, log, "list", "--json", "list")
    capture_json(session, log, "show", "--json", "show", first["id"])
    capture_json(
        session,
        log,
        "update",
        "--json",
        "update",
        first["id"],
        "--title",
        "Fix auth middleware",
        "--body",
        "Details",
    )
    capture_json(session, log, "claim", "--json", "claim", first["id"], "--actor", "alice")
    capture_json(session, log, "show claimed", "--json", "show", first["id"])
    capture_json(session, log, "release", "--json", "release", first["id"], "--actor", "alice")
    capture_json(session, log, "dep add", "--json", "dep", "add", first["id"], second["id"])
    capture_json(session, log, "show blocked", "--json", "show", second["id"])
    capture_json(session, log, "show blocker", "--json", "show", first["id"])
    capture_json(session, log, "ready", "--json", "ready")
    capture_json(session, log, "graph", "--json", "graph")
    capture_json(session, log, "stats", "--json", "stats")
    capture_json(session, log, "close", "--json", "close", first["id"], "--reason", "Fixed in abc1234")
    capture_json(session, log, "ready after close", "--json", "ready")
    capture_json(session, log, "search", "--json", "search", "fix")
    capture_json(session, log, "closed list", "--json", "list", "--status", "closed")
    capture_json(session, log, "reopen", "--json", "update", first["id"], "--status", "open")
    capture_json(session, log, "close again", "--json", "close", first["id"])
    capture_json(session, log, "reopen in progress", "--json", "update", first["id"], "--status", "in_progress")
    capture_json(session, log, "delete", "--json", "delete", second["id"])
    capture_json(session, log, "final list", "--json", "list")
    return log


def test_crud_and_graph_conform(sessions):
    ticket_session, beans_session = sessions
    assert crud_scenario(ticket_session) == crud_scenario(beans_session)


def error_scenario(session):
    log = []
    capture_error(session, log, "show missing", "--json", "show", "bean-00000000")
    capture_error(session, log, "update missing", "--json", "update", "bean-00000000", "--title", "Nope")
    capture_error(session, log, "delete missing", "--json", "delete", "bean-00000000")
    capture_error(session, log, "close missing", "--json", "close", "bean-00000000")
    capture_error(session, log, "claim missing", "--json", "claim", "bean-00000000", "--actor", "alice")
    capture_error(session, log, "release missing", "--json", "release", "bean-00000000", "--actor", "alice")

    first = capture_json(session, log, "create first", "--json", "create", "Task A")
    second = capture_json(session, log, "create second", "--json", "create", "Task B")

    capture_json(session, log, "claim first", "--json", "claim", first["id"], "--actor", "alice")
    capture_error(session, log, "claim twice", "--json", "claim", first["id"], "--actor", "bob")
    capture_error(session, log, "release wrong actor", "--json", "release", first["id"], "--actor", "bob")
    capture_error(
        session,
        log,
        "dep remove missing",
        "--json",
        "dep",
        "remove",
        "bean-aaaaaaaa",
        "bean-bbbbbbbb",
    )
    capture_error(session, log, "unknown type", "--json", "create", "Bad", "--type", "invalid")
    capture_error(session, log, "invalid priority", "--json", "create", "Bad", "--priority", "5")
    capture_error(session, log, "invalid status", "--json", "update", first["id"], "--status", "deleted")
    capture_error(session, log, "missing parent", "--json", "create", "Bad", "--parent", "bean-00000000")

    capture_json(session, log, "dep add", "--json", "dep", "add", first["id"], second["id"])
    capture_error(session, log, "cycle", "--json", "dep", "add", second["id"], first["id"])
    capture_error(session, log, "self cycle", "--json", "dep", "add", first["id"], first["id"])
    capture_error(session, log, "ready filters", "--json", "ready", "--assignee", "alice", "--unassigned")
    capture_error(session, log, "release no target", "--json", "release", "--actor", "alice")
    capture_error(session, log, "release both", "--json", "release", first["id"], "--mine", "--actor", "alice")

    parent = capture_json(session, log, "create parent", "--json", "create", "Epic", "--type", "epic")
    capture_json(session, log, "create child", "--json", "create", "Child", "--parent", parent["id"])
    capture_error(session, log, "close parent", "--json", "close", parent["id"])
    capture_json(session, log, "force close parent", "--json", "close", parent["id"], "--force")
    return log


def test_errors_conform(sessions):
    ticket_session, beans_session = sessions
    assert error_scenario(ticket_session) == error_scenario(beans_session)


def dry_run_scenario(session):
    log = []
    bean = capture_json(session, log, "create", "--json", "create", "Fix auth")
    capture_json(session, log, "dry run create", "--json", "--dry-run", "create", "Temporary")
    capture_json(session, log, "list after dry create", "--json", "list")
    capture_json(session, log, "dry run update", "--json", "--dry-run", "update", bean["id"], "--title", "Temporary")
    capture_json(session, log, "show after dry update", "--json", "show", bean["id"])
    capture_json(session, log, "dry run close", "--json", "--dry-run", "close", bean["id"])
    capture_json(session, log, "show after dry close", "--json", "show", bean["id"])
    capture_json(session, log, "dry run claim", "--json", "--dry-run", "claim", bean["id"], "--actor", "alice")
    capture_json(session, log, "show after dry claim", "--json", "show", bean["id"])
    capture_json(session, log, "dry run delete", "--json", "--dry-run", "delete", bean["id"])
    capture_json(session, log, "show after dry delete", "--json", "show", bean["id"])
    return log


def test_dry_run_conforms(sessions):
    ticket_session, beans_session = sessions
    assert dry_run_scenario(ticket_session) == dry_run_scenario(beans_session)


def filters_scenario(session):
    log = []
    parent = capture_json(session, log, "create parent", "--json", "create", "Epic", "--type", "epic")
    child = capture_json(
        session,
        log,
        "create child",
        "--json",
        "create",
        "Child",
        "--parent",
        parent["id"],
        "--body",
        "searchable body",
    )
    capture_json(session, log, "create other", "--json", "create", "Other")

    capture_json(session, log, "list parent", "--json", "list", "--parent", parent["id"])
    capture_json(session, log, "ready parent", "--json", "ready", "--parent", parent["id"])
    capture_json(session, log, "search body", "--json", "search", "searchable")
    capture_json(session, log, "fields list", "--json", "--fields", "id,title", "list")
    capture_json(session, log, "fields show", "--json", "--fields", "id,title", "show", child["id"])
    capture_json(session, log, "fields ready", "--json", "--fields", "id,title", "ready")
    capture_json(session, log, "fields search", "--json", "--fields", "id,title", "search", "child")

    session.env["MAGIC_BEANS_PARENT_ID"] = parent["id"]
    capture_json(session, log, "env create", "--json", "create", "Scoped")
    capture_json(session, log, "env list", "--json", "list")
    capture_json(session, log, "env ready", "--json", "ready")
    capture_json(session, log, "explicit parent override", "--json", "create", "Unscoped", "--parent", child["id"])
    del session.env["MAGIC_BEANS_PARENT_ID"]
    return log


def test_filters_and_env_parent_conform(sessions):
    ticket_session, beans_session = sessions
    assert filters_scenario(ticket_session) == filters_scenario(beans_session)


def test_non_ascii_output_conforms(sessions):
    ticket_session, beans_session = sessions
    title = "Café ☕"
    body = "Résumé — naïve"

    def scenario(session):
        log = []
        created = capture_text(session, log, "create unicode", "--json", "create", title, "--body", body)
        bean_id = json.loads(created.stdout)["id"]
        capture_text(session, log, "list unicode", "--json", "list")
        capture_text(session, log, "show unicode", "--json", "show", bean_id)
        capture_text(session, log, "search unicode", "--json", "search", "naïve")
        capture_text(session, log, "update unicode", "--json", "update", bean_id, "--title", "日本語")
        capture_text(session, log, "show updated", "--json", "show", bean_id)

        # pydantic renders non-ASCII text literally, without JSON \u escapes.
        assert title in created.stdout
        assert body in created.stdout
        assert "\\u" not in created.stdout
        return log

    assert scenario(ticket_session) == scenario(beans_session)


def test_schema_shape_conforms(sessions):
    ticket_session, beans_session = sessions
    _, ticket_schema = ticket_session.json("--json", "schema")
    _, beans_schema = beans_session.json("--json", "schema")

    assert set(ticket_schema) == set(beans_schema)
    for model in ("Bean", "Dep", "Error"):
        assert set(ticket_schema[model]["properties"]) == set(beans_schema[model]["properties"])
        assert ticket_schema[model]["required"] == beans_schema[model]["required"]


def test_init_dir_and_discovery_conform(sessions):
    ticket_session, beans_session = sessions
    for session in (ticket_session, beans_session):
        result = session.run("init", "--dir")
        assert result.returncode == 0
        assert (session.cwd / ".beans" / "beans.db").exists()
        assert (session.cwd / ".beans" / ".gitignore").read_text() == "*\n!.gitignore\n!AGENTS.md\n!journal.jsonl\n"

    ticket_log = []
    beans_log = []
    for session, log in ((ticket_session, ticket_log), (beans_session, beans_log)):
        capture_json(session, log, "create", "--json", "create", "Discovered")
        capture_json(session, log, "list", "--json", "list")
        capture_json(session, log, "ready", "--json", "ready")
    assert ticket_log == beans_log


def test_config_and_types_conform(sessions):
    ticket_session, beans_session = sessions
    ticket_log = []
    beans_log = []
    for session, log in ((ticket_session, ticket_log), (beans_session, beans_log)):
        capture_text(session, log, "config", "config")
        capture_text(session, log, "types", "types")
        capture_text(session, log, "types add", "types", "add", "spike", "--description", "Investigation")
        capture_text(session, log, "types list", "types")
        capture_text(session, log, "types remove", "types", "remove", "bug")
        capture_text(session, log, "types after remove", "types")
        capture_json(session, log, "create custom", "--json", "create", "Custom type", "--type", "spike")
        capture_error(session, log, "create removed type", "--json", "create", "Removed", "--type", "bug")
    assert ticket_log == beans_log


def test_registry_project_and_migrate_conform(sessions):
    ticket_session, beans_session = sessions
    ticket_log = []
    beans_log = []

    for session, log in ((ticket_session, ticket_log), (beans_session, beans_log)):
        capture_text(session, log, "init registry", "init", "--name", "myblog")
        capture_json(session, log, "project create", "--project", "myblog", "--json", "create", "First post")
        capture_json(session, log, "project list", "--project", "myblog", "--json", "list")
        capture_text(session, log, "config after init", "config")
        capture_text(session, log, "migrate", "migrate", "--name", "migrated", input="n\n")
        capture_text(session, log, "config after migrate", "config")

    assert ticket_log == beans_log


def test_journal_export_and_rebuild_conform(sessions):
    ticket_session, beans_session = sessions
    ticket_log = []
    beans_log = []

    for session, log in ((ticket_session, ticket_log), (beans_session, beans_log)):
        bean = capture_json(session, log, "create", "--json", "create", "Fix auth")
        capture_json(session, log, "update", "--json", "update", bean["id"], "--title", "Updated")
        capture_json(session, log, "close", "--json", "close", bean["id"])
        export = capture_text(session, log, "export", "export-journal")
        journal_path = session.root / "journal.jsonl"
        journal_path.write_text(export.stdout)
        target = session.root / "rebuilt.db"
        capture_text(session, log, "rebuild", "--db", str(target), "rebuild", str(journal_path))
        capture_json(session, log, "rebuilt list", "--db", str(target), "--json", "list")

    assert ticket_log == beans_log


def test_help_lists_same_commands(sessions):
    ticket_session, beans_session = sessions
    ticket_help = ticket_session.run("--help").stdout
    beans_help = beans_session.run("--help").stdout

    commands = {
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
    }
    for command in commands:
        assert command in ticket_help
        assert command in beans_help


def test_source_uses_stdlib_only():
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
    assert "magic_beans" not in source and "import beans" not in source


def test_no_timestamp_normalization_gap():
    # Guard the normalizer used by the conformance tests.
    assert TIMESTAMP_RE.sub("<ts>", "2025-01-01T00:00:00.123456Z") == "<ts>"
    assert TIMESTAMP_RE.sub("<ts>", "2025-01-01 00:00:00+00:00") == "<ts>"
    assert normalize("task-deadbeef", {}) == "<id1>"

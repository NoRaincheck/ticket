from __future__ import annotations

from datetime import UTC, datetime
import re
import sqlite3

import pytest

import ticket
from ticket import (
    Bean,
    BeanId,
    BeanNotFoundError,
    Config,
    CyclicDepError,
    Dep,
    DepNotFoundError,
    OpenChildrenError,
    Project,
    Store,
    add_dep,
    claim_bean,
    close_bean,
    create_bean,
    delete_bean,
    graph,
    init_project,
    init_project_local,
    list_beans,
    list_deps,
    normalize_git_remote,
    ready_beans,
    release_bean,
    release_mine,
    remove_dep,
    reopen_bean,
    search_beans,
    stats,
    update_bean,
)


@pytest.fixture()
def store():
    with Store(sqlite3.connect(":memory:")) as session:
        yield session


class TestBeanId:
    def test_generate_is_type_prefixed(self):
        bean_id = BeanId.generate("epic")
        assert re.fullmatch(r"epic-[0-9a-f]{8}", bean_id)
        assert bean_id.type_prefix == "epic"

    def test_rejects_invalid_values(self):
        with pytest.raises(ValueError, match="Invalid bean id"):
            BeanId("not-a-bean-id")

    def test_accepts_any_lowercase_prefix(self):
        assert BeanId("spike-deadbeef").type_prefix == "spike"


class TestBeanModel:
    def test_defaults_and_json_shape(self):
        bean = Bean(id="bean-00000000", title="Fix auth", created_at=datetime(2025, 1, 1, tzinfo=UTC))
        assert bean.model_dump() == {
            "id": "bean-00000000",
            "title": "Fix auth",
            "type": "task",
            "status": "open",
            "priority": 2,
            "body": "",
            "parent_id": None,
            "assignee": None,
            "created_by": None,
            "ref_id": None,
            "created_at": datetime(2025, 1, 1, tzinfo=UTC),
            "closed_at": None,
            "close_reason": None,
        }
        assert '"created_at":"2025-01-01T00:00:00Z"' in bean.model_dump_json()

    def test_validates_status_and_priority(self):
        with pytest.raises(ValueError):
            Bean(title="Bad", status="deleted")
        with pytest.raises(ValueError):
            Bean(title="Bad", priority=5)

    def test_parses_iso_timestamps(self):
        bean = Bean(title="Fix", created_at="2025-01-01T00:00:00Z", closed_at="2025-01-02T00:00:00+00:00")
        assert bean.created_at == datetime(2025, 1, 1, tzinfo=UTC)
        assert bean.closed_at == datetime(2025, 1, 2, tzinfo=UTC)


class TestStore:
    def test_roundtrip_preserves_fields(self, store):
        bean = Bean(
            title="Full bean",
            type="epic",
            status="in_progress",
            priority=0,
            body="Some details",
            parent_id="bean-00000000",
            assignee="alice",
            created_by="bob",
            ref_id="GH-42",
        )
        store.create(bean)
        assert store.get(bean.id) == bean

    def test_update_and_delete(self, store):
        bean = store.create(Bean(title="Old title"))
        assert store.update(bean.id, title="New title") == 1
        assert store.get(bean.id).title == "New title"
        assert store.delete(bean.id) == 1
        with pytest.raises(BeanNotFoundError):
            store.get(bean.id)

    def test_ready_respects_transitive_blocks_and_children(self, store):
        first = store.create(Bean(title="First"))
        second = store.create(Bean(title="Second"))
        third = store.create(Bean(title="Third"))
        store.add_dep(Dep(from_id=first.id, to_id=second.id))
        store.add_dep(Dep(from_id=second.id, to_id=third.id))

        assert store.ready() == [first]
        store.update(first.id, status="closed")
        assert store.ready() == [second]

        parent = store.create(Bean(title="Parent"))
        store.create(Bean(title="Child", parent_id=parent.id))
        assert parent not in store.ready()

    def test_journal_export_and_replay(self, store):
        bean = store.create(Bean(title="Fix auth"))
        store.update(bean.id, title="Updated")
        lines = list(store.journal.export())
        assert [entry["action"] for entry in map(ticket.json.loads, lines)] == ["create", "update"]

        with Store(sqlite3.connect(":memory:")) as target:
            target.journal.replay(lines)
            assert target.get(bean.id).title == "Updated"


class TestApi:
    def test_create_show_update_and_close(self, store):
        bean = create_bean(store, "Fix auth", body="Details")
        assert bean.id.startswith("task-")
        assert bean.body == "Details"

        updated = update_bean(store, bean.id, title="New title", priority=0)
        assert updated.title == "New title"
        assert updated.priority == 0

        closed = close_bean(store, bean.id, reason="Done")
        assert closed.status == "closed"
        assert closed.close_reason == "Done"
        assert closed.closed_at is not None

        reopened = reopen_bean(store, bean.id)
        assert reopened.status == "open"
        assert reopened.closed_at is None
        assert reopened.close_reason is None

    def test_create_with_deps_and_parent_validation(self, store):
        blocker = create_bean(store, "Blocker")
        blocked = create_bean(store, "Blocked", deps=[blocker.id])
        assert blocked not in ready_beans(store)
        close_bean(store, blocker.id)
        assert blocked in ready_beans(store)

        with pytest.raises(BeanNotFoundError, match="does not exist"):
            create_bean(store, "Orphan", parent_id="bean-00000000")

    def test_create_validates_configured_types(self, store):
        with pytest.raises(ValueError, match="Unknown type: spike"):
            create_bean(store, "Bad", type="spike", valid_types={"task", "bug", "epic"})
        assert create_bean(store, "Any", type="spike").type == "spike"

    def test_close_children_guard(self, store):
        parent = create_bean(store, "Epic")
        create_bean(store, "Task", parent_id=parent.id)
        with pytest.raises(OpenChildrenError, match="1 open child remain"):
            close_bean(store, parent.id)
        assert close_bean(store, parent.id, force=True).status == "closed"

    def test_claim_release_and_release_mine(self, store):
        first = create_bean(store, "Task A")
        second = create_bean(store, "Task B")
        assert claim_bean(store, first.id, "alice").assignee == "alice"
        assert claim_bean(store, first.id, "alice") == claim_bean(store, first.id, "alice")
        with pytest.raises(ValueError, match="already claimed"):
            claim_bean(store, first.id, "bob")
        with pytest.raises(ValueError, match="claimed by alice"):
            release_bean(store, first.id, "bob")

        claim_bean(store, second.id, "alice")
        released = release_mine(store, "alice")
        assert len(released) == 2
        assert all(bean.assignee is None and bean.status == "open" for bean in released)

    def test_delete_returns_bean_and_removes_deps(self, store):
        first = create_bean(store, "Task A")
        second = create_bean(store, "Task B")
        add_dep(store, first.id, second.id)
        assert delete_bean(store, first.id) == first
        assert list_deps(store, first.id) == []
        with pytest.raises(BeanNotFoundError):
            delete_bean(store, first.id)

    def test_dependencies_detect_cycles(self, store):
        first = create_bean(store, "Alpha")
        second = create_bean(store, "Beta")
        third = create_bean(store, "Gamma")
        add_dep(store, first.id, second.id)
        add_dep(store, second.id, third.id)

        with pytest.raises(CyclicDepError, match="Alpha"):
            add_dep(store, third.id, first.id)
        with pytest.raises(CyclicDepError, match="cycle"):
            add_dep(store, first.id, first.id)
        with pytest.raises(DepNotFoundError):
            remove_dep(store, second.id, first.id)

    def test_filters_search_stats_and_graph(self, store):
        first = create_bean(store, "Fix auth", type="bug", body="authentication")
        second = create_bean(store, "Add tests", priority=0)
        add_dep(store, first.id, second.id)

        assert list_beans(store, types=["bug"]) == [first]
        assert search_beans(store, "auth") == [first]
        assert stats(store)["by_type"] == {"bug": 1, "task": 1}
        graph_data = graph(store)
        assert len(graph_data["nodes"]) == 2
        assert graph_data["edges"][0]["from_id"] == first.id


class TestConfigAndWorkspace:
    def test_config_roundtrip(self, tmp_path):
        path = tmp_path / "config.json"
        config = Config(
            path=path,
            projects=[Project(name="app", identifier="github.com/me/app", store="/store/app")],
        )
        config.add_type(ticket.BeanType(name="spike", description="Investigation"))
        config.save()

        loaded = Config.from_path(path)
        assert loaded.projects == config.projects
        assert loaded.type_names() == {"task", "bug", "epic", "spike"}
        assert "path" not in path.read_text()

    def test_init_project_local_and_discovery(self, tmp_path, monkeypatch):
        project = tmp_path / "myproject"
        project.mkdir()
        beans_dir = init_project_local(cwd=project)
        assert beans_dir == project / ".beans"
        assert (beans_dir / "beans.db").exists()
        assert (beans_dir / ".gitignore").read_text() == ticket.GITIGNORE_CONTENT

        monkeypatch.delenv("MAGIC_BEANS_DIR", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        assert ticket.find_beans_dir(start=project) == beans_dir

    def test_init_project_registry(self, tmp_path):
        project = tmp_path / "myproject"
        project.mkdir()
        config_file = tmp_path / "config" / "config.json"
        store_dir = init_project(cwd=project, name="myblog", data_base=tmp_path / "data", config_file=config_file)

        assert (store_dir / "beans.db").exists()
        config = Config.from_path(config_file)
        assert config.find_by_name("myblog") is not None
        assert not (project / ".beans").exists()

    def test_normalize_git_remote(self):
        assert normalize_git_remote("git@github.com:org/repo.git") == "github.com/org/repo"
        assert normalize_git_remote("https://gitlab.com/team/project/") == "gitlab.com/team/project"
        assert ticket.detect_name("gitlab.com/org/sub/repo") == "repo"

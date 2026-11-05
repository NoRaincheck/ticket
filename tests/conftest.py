from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
TICKET = ROOT / "ticket.py"

ID_RE = re.compile(r"\b[a-z]+-[0-9a-f]{8}\b")
TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")


def normalize(value, ids: dict[str, str], root: Path | None = None):
    if isinstance(value, dict):
        return {key: normalize(item, ids, root) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize(item, ids, root) for item in value]
    if isinstance(value, str):
        if root is not None:
            value = value.replace(str(root), "<root>")
        value = TIMESTAMP_RE.sub("<timestamp>", value)
        if ID_RE.fullmatch(value):
            return ids.setdefault(value, f"<id{len(ids) + 1}>")
        return ID_RE.sub(lambda match: ids.setdefault(match.group(0), f"<id{len(ids) + 1}>"), value)
    return value


class CliSession:
    def __init__(self, command: list[str], root: Path):
        self.command = command
        self.root = root
        self.cwd = root / "work"
        self.cwd.mkdir(parents=True)
        self.ids: dict[str, str] = {}
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(root / "home"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
            }
        )
        self.env.pop("MAGIC_BEANS_DIR", None)
        self.env.pop("MAGIC_BEANS_PARENT_ID", None)

    def run(self, *args: str, input: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self.command, *args],
            cwd=self.cwd,
            env=self.env,
            input=input,
            text=True,
            capture_output=True,
            timeout=30,
        )

    def json(self, *args: str) -> tuple[int, object]:
        result = self.run(*args)
        output = result.stdout.strip()
        return result.returncode, json.loads(output) if output else None

    def normalized(self, value):
        return normalize(value, self.ids, self.root)


@pytest.fixture()
def sessions(tmp_path):
    ticket = CliSession([sys.executable, str(TICKET)], tmp_path / "ticket")
    beans = CliSession([sys.executable, "-c", "from beans.cli import app; app()"], tmp_path / "beans")
    assert ticket.run("init", "--dir").returncode == 0
    assert beans.run("init", "--dir").returncode == 0
    return ticket, beans

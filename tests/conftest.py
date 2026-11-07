from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
TICKET = ROOT / "ticket.py"


class CliSession:
    def __init__(self, root: Path):
        self.root = root
        self.cwd = root / "work"
        self.cwd.mkdir(parents=True)
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(root / "home"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
            }
        )
        self.env.pop("TICKET_DIR", None)
        self.env.pop("TICKET_PARENT_ID", None)

    def run(self, *args: str, input: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(TICKET), *args],
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


@pytest.fixture()
def cli(tmp_path):
    return CliSession(tmp_path / "ticket")


@pytest.fixture()
def initialized_cli(cli):
    result = cli.run("init", "--dir")
    assert result.returncode == 0, result.stderr
    return cli

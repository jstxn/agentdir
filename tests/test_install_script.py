from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_installer_prints_safe_gitignore_choices() -> None:
    text = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert "agentdir adopt --if-needed --gitignore user" in text
    assert "--gitignore project" in text
    assert "--gitignore none" in text

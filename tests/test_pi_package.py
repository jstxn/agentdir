from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_pi_package_manifest_points_to_agentdir_skill() -> None:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))

    assert package["name"] == "@jstxn/agentdir"
    assert package["version"] == "0.7.3"
    assert "pi-package" in package["keywords"]
    assert package["pi"] == {
        "skills": ["./skills"],
        "image": "https://raw.githubusercontent.com/jstxn/agentdir/v0.7.3/docs/assets/agentdir-overview.png",
    }
    assert "skills" in package["files"]
    assert "docs/PI_PACKAGE.md" in package["files"]
    assert (ROOT / "skills" / "agentdir" / "SKILL.md").is_file()


def test_agentdir_pi_skill_frontmatter_and_workflow() -> None:
    text = (ROOT / "skills" / "agentdir" / "SKILL.md").read_text(encoding="utf-8")

    assert text.startswith("---\n")
    frontmatter = text.split("---\n", 2)[1]
    description = re.search(r"^description: (.+)$", frontmatter, re.MULTILINE)

    assert re.search(r"^name: agentdir$", frontmatter, re.MULTILINE)
    assert description is not None
    assert len(description.group(1)) <= 1024
    assert re.search(r"^compatibility: ", frontmatter, re.MULTILINE)
    assert "agentdir work start" in text
    assert "agentdir run --" in text
    assert "agentdir work finish --json" in text

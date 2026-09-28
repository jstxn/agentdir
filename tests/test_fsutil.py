from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from agentdir.fsutil import atomic_write_text


def test_concurrent_atomic_writes_to_one_path_do_not_collide(tmp_path) -> None:
    # The memory daemon and the CLI that starts it write the same state file
    # at the same time; each writer needs its own temp file.
    target = tmp_path / "state.json"

    def write(n: int) -> None:
        for i in range(25):
            atomic_write_text(target, f"{n}:{i}\n")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(8)))

    assert target.read_text(encoding="utf-8").endswith(":24\n")
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

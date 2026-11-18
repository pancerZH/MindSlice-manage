"""A stand-in for agy: round n copies <dir>/proposal-<n>.toml (if there is one) to ./proposal.toml.

argv: <dir> <prompt>. It also records each prompt and checks that it only sees the snapshot."""

import json
import shutil
import sys
from pathlib import Path

d = Path(sys.argv[1])
n = len(list(d.glob("prompt-seen-*")))
(d / f"prompt-seen-{n}").write_text(sys.argv[2], encoding="utf-8")
assert Path("repo").is_dir() and not Path("repo/.git").exists(), "the session should see a snapshot without .git"
assert json.loads(Path("input.json").read_text())["repo"] == "repo"
src = d / f"proposal-{n}.toml"
if src.is_file():
    shutil.copy(src, "proposal.toml")
Path("repo/LOCAL_EDIT").write_text("the snapshot may be changed; the real repo must not be")
print(json.dumps({"conversation_id": "conv-1", "response": "done"}))

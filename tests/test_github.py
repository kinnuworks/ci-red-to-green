import io
import json

from crg.github import GitHub


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_commit_uses_noreply_identity():
    calls = []

    def opener(req, timeout=None):
        path = req.full_url.split("api.github.com")[-1]
        body = json.loads(req.data) if req.data else None
        calls.append((req.get_method(), path, body))
        assert req.get_header("Authorization") == "Bearer tok"
        out = {
            "/user": {"login": "alice", "id": 42},
            "/repos/o/r/git/commits/base": {"tree": {"sha": "t0"}},
        }.get(path) or {"sha": "s1"}
        return Resp(json.dumps(out).encode())

    gh = GitHub("tok", "o/r", opener=opener)
    sha = gh.create_branch_commit("base", {"src/a.py": b"x", "gone.py": None}, "msg", "ci-fix/1")
    assert sha == "s1"
    commit = [b for m, p, b in calls if m == "POST" and p.endswith("/git/commits")][0]
    assert commit["author"] == {"name": "alice", "email": "42+alice@users.noreply.github.com"}
    tree = [b for m, p, b in calls if p.endswith("/git/trees")][0]
    assert {"path": "gone.py", "mode": "100644", "type": "blob", "sha": None} in tree["tree"]
    assert any(p.endswith("/git/refs") and b["ref"] == "refs/heads/ci-fix/1" for m, p, b in calls if b)

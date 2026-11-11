"""A small GitHub REST client: Actions runs and logs, the git data API, pull requests.

Standard library only. The token comes from Hermes's secret store
(agent.secret_scope.get_secret); this module never reads it from anywhere else.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
TIMEOUT = 30


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub {status}: {message}")
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # we follow log redirects by hand, without the token
        return None


class GitHub:
    def __init__(self, token: str, repo: str, opener=None):
        if "/" not in repo:
            raise ValueError("repo must look like owner/name")
        self.token = token
        self.repo = repo
        self._open = opener or urllib.request.build_opener(_NoRedirect()).open

    # ---- transport -----------------------------------------------------------
    def _req(self, method: str, path: str, body: dict | None = None, raw: bool = False):
        url = path if path.startswith("http") else f"{API}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "hermes-ci-red-to-green")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._open(req, timeout=TIMEOUT) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and raw:
                loc = e.headers.get("Location", "")
                return self._download(loc)
            msg = e.read().decode("utf-8", "replace")[:300]
            raise GitHubError(e.code, msg) from None
        if raw:
            return payload.decode("utf-8", "replace")
        return json.loads(payload) if payload else {}

    def _download(self, url: str) -> str:
        # Signed blob URL: no Authorization header may follow us there.
        if not url.startswith("https://"):
            raise GitHubError(0, "log redirect was not https")
        req = urllib.request.Request(url, headers={"User-Agent": "hermes-ci-red-to-green"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read().decode("utf-8", "replace")

    # ---- Actions -------------------------------------------------------------
    def runs(self, branch: str | None = None, head_sha: str | None = None, per_page: int = 10) -> list[dict]:
        q = {"per_page": str(per_page)}
        if branch:
            q["branch"] = branch
        if head_sha:
            q["head_sha"] = head_sha
        out = self._req("GET", f"/repos/{self.repo}/actions/runs?{urllib.parse.urlencode(q)}")
        return out.get("workflow_runs", [])

    def failed_jobs(self, run_id: int) -> list[dict]:
        out = self._req("GET", f"/repos/{self.repo}/actions/runs/{run_id}/jobs?per_page=50")
        return [j for j in out.get("jobs", []) if j.get("conclusion") == "failure"]

    def job_log(self, job_id: int) -> str:
        return self._req("GET", f"/repos/{self.repo}/actions/jobs/{job_id}/logs", raw=True)

    def tree(self, sha: str) -> dict[str, str]:
        """path -> blob sha for every file at a commit."""
        commit = self._req("GET", f"/repos/{self.repo}/git/commits/{sha}")
        tree = self._req("GET", f"/repos/{self.repo}/git/trees/{commit['tree']['sha']}?recursive=1")
        return {e["path"]: e["sha"] for e in tree.get("tree", []) if e.get("type") == "blob"}

    def blob(self, sha: str) -> bytes:
        out = self._req("GET", f"/repos/{self.repo}/git/blobs/{sha}")
        return base64.b64decode(out.get("content", ""))

    def compare(self, base: str, head: str) -> list[dict]:
        """Commits in head that aren't in base, oldest first."""
        out = self._req("GET", f"/repos/{self.repo}/compare/{base}...{head}")
        return out.get("commits", [])

    def commit(self, sha: str) -> dict:
        return self._req("GET", f"/repos/{self.repo}/commits/{sha}")

    # ---- git data + PR -----------------------------------------------------------
    def create_branch_commit(self, base_sha: str, files: dict[str, bytes | None], message: str, branch: str) -> str:
        """Commit `files` (path -> new bytes, or None to delete) on top of base_sha as a new branch.

        Returns the new commit sha."""
        base = self._req("GET", f"/repos/{self.repo}/git/commits/{base_sha}")
        tree = []
        for path, content in sorted(files.items()):
            if content is None:
                tree.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                continue
            blob = self._req("POST", f"/repos/{self.repo}/git/blobs",
                             {"content": base64.b64encode(content).decode(), "encoding": "base64"})
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        new_tree = self._req("POST", f"/repos/{self.repo}/git/trees", {"base_tree": base["tree"]["sha"], "tree": tree})
        body = {"message": message, "tree": new_tree["sha"], "parents": [base_sha]}
        who = self.noreply_identity()
        if who:
            body["author"] = body["committer"] = who
        c = self._req("POST", f"/repos/{self.repo}/git/commits", body)
        self._req("POST", f"/repos/{self.repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": c["sha"]})
        return c["sha"]

    def noreply_identity(self) -> dict | None:
        """The token owner's GitHub no-reply address, so fix commits never expose a private email."""
        try:
            me = self._req("GET", "/user")
            return {"name": me["login"], "email": f"{me['id']}+{me['login']}@users.noreply.github.com"}
        except (GitHubError, KeyError, OSError):
            return None

    def open_pr(self, head: str, base: str, title: str, body: str) -> dict:
        return self._req("POST", f"/repos/{self.repo}/pulls", {"title": title, "head": head, "base": base, "body": body})

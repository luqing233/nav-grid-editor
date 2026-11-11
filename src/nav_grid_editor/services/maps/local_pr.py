"""Snapshot saved navigation edits and publish a regular data PR in isolation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import threading
from urllib.parse import quote
import uuid

from ...integrations.github_accounts import GitHubAccount, list_accounts
from .pr_merge import PRMergeSession, json_bytes, safe_path
from .pr_publish import IntegrationPublisher
from .pr_review import DataPRReview, compare_grid
from .grid_merge import merge_grid, merge_records, merge_dataset


def navigation_path(path):
    return "/" not in path and "\\" not in path and (path.endswith(".grid.npz") or path in ("links.json", "levels.json", "dataset.json"))


def digest(value):
    return hashlib.sha256(value).hexdigest() if value is not None else None


class LocalSnapshot(PRMergeSession):
    def __init__(self, review, account, paths=None):
        self.review = review
        self.account_login = account.login
        self.repository = review._repository_name()
        repo = account.api("repos/" + self.repository)
        if not repo.get("permissions", {}).get("push"):
            raise ValueError("所选账号没有数据仓库推送权限；请选有权限的账号。本入口暂不自动创建 fork。")
        self.branch = repo["default_branch"]
        self.remote_target = account.api(f"repos/{self.repository}/commits/{quote(self.branch, safe='')}")["sha"]
        if not re.fullmatch(r"[0-9a-f]{40}", self.remote_target):
            raise ValueError("GitHub 提交标识非法")
        review._git("fetch", "--no-tags", f"https://github.com/{self.repository}.git", self.remote_target)
        self.local_head = review._git("rev-parse", "HEAD").decode().strip()
        bases = review._git("merge-base", "--all", self.local_head, self.remote_target).decode().split()
        if len(bases) != 1:
            raise ValueError("无法确定唯一共同祖先，请先整理本地数据分支")
        self.target = bases[0]
        self.token = uuid.uuid4().hex
        self.directory = review.cache.parent / "local-pr" / self.token
        self.directory.mkdir(parents=True)
        self.trees, self.blobs, self.choices = {}, {}, {}
        self.bytes_read = 0
        self.prs, self.result, self.fingerprints = [], {}, {}
        self.response = {"ready": True}
        candidates = set(review._git("diff", "--name-only", "--no-renames", "-z", self.target).decode().split("\0"))
        candidates.update(review._git("ls-files", "--others", "--exclude-standard", "-z").decode().split("\0"))
        candidates.discard("")
        allowed = sorted(p for p in candidates if navigation_path(p))
        if paths is not None:
            if not isinstance(paths, list) or not paths or any(not isinstance(p, str) or p not in allowed for p in paths):
                raise ValueError("请选择当前已修改的导航文件")
            allowed = sorted(set(paths))
        if len(allowed) > 100 or sum(p.endswith(".grid.npz") for p in allowed) > 20:
            raise ValueError("一次最多提交 20 份网格、100 份导航文件")
        self.files = []
        saved_bytes = 0
        for path in allowed:
            content = self.saved(path)
            saved_bytes += len(content) if content is not None else 0
            if saved_bytes > 256 * 1024 * 1024:
                raise ValueError("本地导航快照超过 256 MiB，请减少所选文件")
            self.fingerprints[path] = digest(content)
            before = self.read(self.target, path)
            remote = self.read(self.remote_target, path)
            if content == before:
                continue
            count = None
            # Omit edits already incorporated upstream. Never interpret an older
            # local snapshot as a request to erase somebody else's newer edits.
            if path.endswith(".grid.npz"):
                b, m, p = (self.grid(v) for v in (before, remote, content))
                merged, conflicts = merge_grid(b, m, p, {}, path)
                diff = compare_grid(m, merged) if m or merged else None
                if not conflicts and (diff is None or not diff["changes"] and not diff["metadata_changes"]):
                    continue
                comparison = compare_grid(b, p)
                count = len(comparison["changes"])
            else:
                docs = [json.loads(v) if v is not None else None for v in (before, remote, content)]
                if path == "dataset.json":
                    merged, conflicts = merge_dataset(*docs, {}, path)
                    normalized_remote, _ = merge_dataset(docs[1], docs[1], docs[1], {}, path)
                else:
                    merged, conflicts = merge_records(*docs, "links" if path=="links.json" else "layers", {}, path)
                    normalized_remote = docs[1]
                if not conflicts and merged == normalized_remote:
                    continue
            self.result[path] = content
            self.files.append(dict(path=path, changes=count, operation="删除" if content is None else "新增" if before is None else "修改"))
        if not self.result:
            raise ValueError("没有可提交的新修改；请先保存网格，或这些修改已合入目标分支")

    def saved(self, path):
        safe_path(path)
        root = self.review.repository.resolve()
        file = root / path
        if file.is_symlink() or file.resolve().parent != root:
            raise ValueError("不支持符号链接或仓库外文件：" + path)
        if not file.exists():
            return None
        if file.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("导航文件超过 32 MiB：" + path)
        return file.read_bytes()

    def check_fresh(self):
        if self.review._repository_name() != self.repository or self.review._git("rev-parse", "HEAD").decode().strip() != self.local_head:
            raise ValueError("本地仓库版本已变化，请重新检查修改")
        if any(digest(self.saved(p)) != value for p, value in self.fingerprints.items()):
            raise ValueError("检查后的本地文件又有变化，请重新检查修改")


class LocalPublisher(IntegrationPublisher):
    def __init__(self, review, session=None, identity=None, account=None, title=None):
        self.review, self.session, self.account = review, session, account
        self.guard, self.running = threading.Lock(), False
        if session:
            spec = [session.repository, session.target, session.remote_target, account.login, title,
                    sorted((p, digest(v)) for p,v in session.result.items())]
            identity = hashlib.sha256(json_bytes(spec)).hexdigest()[:24]
        if not isinstance(identity,str) or not re.fullmatch(r"[0-9a-f]{24}",identity):
            raise ValueError("发布标识非法")
        self.directory = review.cache.parent / "local-publications" / identity
        self.directory.mkdir(parents=True,exist_ok=True)
        self.state_file = self.directory / "state.json"
        if self.state_file.exists():
            self.state = json.loads(self.state_file.read_bytes())
            if self.state.get("running"):
                self.state.update(running=False, phase="failed", error="上次发布中断，可以重试")
        elif session:
            self.state = dict(id=identity, repository=session.repository, target=session.target, remote_target=session.remote_target,
                              base=session.branch, branch="codex/local-grid-"+identity, prs=[], account=account.login,
                              title=title, files=session.files, phase="idle", running=False, head="", url="", error="")
            # Only non-secret publication metadata is persisted.
            self.save()
        else:
            raise ValueError("发布记录不存在")

    def command_environment(self):
        login=self.account.user["login"]
        email=f"{self.account.user['id']}+{login}@users.noreply.github.com"
        return dict(self.account.environment(),GIT_AUTHOR_NAME=login,GIT_AUTHOR_EMAIL=email,
                    GIT_COMMITTER_NAME=login,GIT_COMMITTER_EMAIL=email)

    def fresh(self):
        if self.review._repository_name()!=self.state["repository"]:
            raise ValueError("数据仓库已切换，请重新检查修改")
        latest = self.account.api(f"repos/{self.state['repository']}/commits/{quote(self.state['base'], safe='')}")["sha"]
        if latest != self.state["remote_target"]:
            raise ValueError("目标分支已更新，请重新检查修改；已有发布分支会保留")
        if self.session:
            self.session.check_fresh()

    def _build(self):
        self.save(phase="validating")
        self.session.export()
        self.work=self.directory/("git-"+uuid.uuid4().hex)
        self.empty_hooks=self.directory/"empty-hooks"
        self.work.mkdir();self.empty_hooks.mkdir(exist_ok=True)
        self.git("init","-q")
        login=self.account.user["login"]
        self.git("config","--local","user.name",login)
        self.git("config","--local","user.email",f"{self.account.user['id']}+{login}@users.noreply.github.com")
        self.git("config","--local","credential.helper","")
        self.git("config","--local","--add","credential.helper","!"+shlex.quote(Path(self.gh_path).as_posix())+" auth git-credential")
        self.git("config","--local","lfs.storage",str((self.work/".git"/"lfs").resolve()))
        self.git("config","--local","lfs.url",f"https://github.com/{self.state['repository']}.git/info/lfs")
        self.git("remote","add","origin",f"https://github.com/{self.state['repository']}.git")
        self.git("fetch","--no-tags","--no-recurse-submodules",str(self.review.repository.resolve()),self.state["target"])
        self.save(phase="committing",work=str(self.work.resolve()))
        tree=self.write_tree(self.session.result,self.state["target"],self.session.tree(self.state["target"]))
        head=self.git("commit-tree",tree,"-p",self.state["target"],data=(self.state["title"]+"\n").encode()).decode().strip()
        self.git("update-ref","refs/heads/"+self.state["branch"],head)
        self.save(head=head)

    def find_pr(self):
        items=json.loads(self.gh("pr","list","--repo",self.state["repository"],"--state","all","--head",self.state["branch"],
                                "--base",self.state["base"],"--json","url,number,headRefOid,body,state","--limit","100"))
        for item in items:
            if "<!-- nav-grid-local:"+self.state["id"]+" -->" in item.get("body",""):
                if item["headRefOid"]!=self.state["head"]:
                    raise ValueError("发布分支已被其他提交更新，请检查 GitHub PR")
                return item
        if items:raise ValueError("该分支已有其它 PR，已停止重复创建")
        return None

    def _create_pr(self):
        self.save(phase="creating_pr")
        existing=self.find_pr()
        if existing:return existing
        self.fresh()
        body=(f"提交本地已保存的导航数据，提交账号：@{self.state['account']}。\n\n"
              +"\n".join("- `"+f["path"]+"`："+f["operation"] for f in self.state["files"])
              +f"\n\n基线：`{self.state['target']}`。NPZ/JSON 结构保持不变，清单大小和校验值已更新。"
              +f"\n\n<!-- nav-grid-local:{self.state['id']} -->\n")
        payload=self.directory/"pull-request.json"
        payload.write_bytes(json_bytes(dict(title=self.state["title"],body=body,head=self.state["branch"],base=self.state["base"])))
        result=json.loads(self.gh("api","--method","POST",f"repos/{self.state['repository']}/pulls","--input",str(payload.resolve())))
        return dict(url=result["html_url"],number=result["number"],state=result["state"])

    def _run(self):
        try:
            self.gh_path=shutil.which("gh")
            self.account=GitHubAccount(self.state["account"])
            repo=self.account.api("repos/"+self.state["repository"])
            if not repo.get("permissions",{}).get("push"):
                raise ValueError("所选账号没有推送权限")
            if self.state.get("head"):
                self.work=Path(self.state["work"]);self.empty_hooks=self.directory/"empty-hooks"
                existing=self.find_pr()
                if existing:
                    self.save(phase="complete",url=existing["url"],number=existing["number"]);return
            self.fresh()
            if not self.state.get("head"):self._build()
            self._push();self.save(pushed=True)
            pr=self._create_pr()
            self.save(phase="complete",url=pr["url"],number=pr["number"])
        except Exception as error:
            self.save(phase="failed",error=self.account.redact(str(error)) if self.account else str(error))
        finally:
            self.save(running=False)
            with self.guard:self.running=False


class LocalPRService:
    def __init__(self, review):
        self.review=review
        self.lock=threading.Lock()
        self.previews,self.jobs={},{}

    accounts=staticmethod(list_accounts)

    def prepare(self, login, paths=None):
        account=GitHubAccount(login)
        reader=DataPRReview(self.review.repository,self.review.cache)
        reader.command_environment=account.environment
        reader.redact=account.redact
        gh=shutil.which("gh") or "gh"
        repository=self.review._repository_name()
        reader.git_options=["-c","credential.helper=","-c","credential.helper=!"+shlex.quote(Path(gh).as_posix())+" auth git-credential",
                            "-c",f"lfs.url=https://github.com/{repository}.git/info/lfs"]
        with self.review._lock:
            snapshot=LocalSnapshot(reader,account,paths)
        with self.lock:
            if len(self.previews)>=4:self.previews.pop(next(iter(self.previews)))
            self.previews[snapshot.token]=snapshot
        return dict(ok=True,token=snapshot.token,account=login,repository=snapshot.repository,base=snapshot.branch,
                    target=snapshot.remote_target,ancestor=snapshot.target,files=snapshot.files)

    def publish(self, token, login, title):
        if not isinstance(title,str) or not 1<=len(title.strip())<=160 or any(c in title for c in "\r\n\0"):
            raise ValueError("请填写 1–160 字的单行 PR 标题")
        with self.lock:
            snapshot=self.previews.get(token)
            if snapshot is None:raise ValueError("检查结果已过期，请重新检查修改")
            if login!=snapshot.account_login:raise ValueError("提交账号已改变，请重新检查修改")
            account=GitHubAccount(login)
            snapshot.check_fresh()
            candidate=LocalPublisher(self.review,snapshot,account=account,title=title.strip())
            publisher=self.jobs.setdefault(candidate.state["id"],candidate)
            return publisher.start()

    def action(self, identity, retry=False):
        with self.lock:
            publisher=self.jobs.get(identity)
            if publisher is None:
                publisher=LocalPublisher(self.review,identity=identity)
                self.jobs[identity]=publisher
            return publisher.start() if retry else publisher.status()

"""Resumable publication of reviewed grid merges, using an isolated Git index."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import threading
import uuid

from .pr_merge import json_bytes, safe_path


class IntegrationPublisher:
    def __init__(self, review, session=None, identity=None):
        self.review, self.session = review, session
        self.guard = threading.Lock()
        self.running = False
        if session is not None:
            specification = dict(version=1, repository=session.repository, target=session.target,
                                 branch=session.branch, prs=session.prs,
                                 choices={c["id"]:c["choice"] for c in session.response["conflicts"]})
            identity = hashlib.sha256(json.dumps(specification,sort_keys=True).encode()).hexdigest()[:24]
        if not isinstance(identity,str) or not re.fullmatch(r"[0-9a-f]{24}",identity):
            raise ValueError("发布记录标识非法")
        self.directory = review.cache.parent / "pr-publications" / identity
        self.directory.mkdir(parents=True,exist_ok=True)
        self.state_file = self.directory / "state.json"
        if self.state_file.is_file():
            self.state = json.loads(self.state_file.read_text(encoding="utf-8"))
            if self.state.get("running"):
                self.state.update(running=False,phase="failed",error="上次发布被中断，可以重试")
        elif session is not None:
            self.state = dict(id=identity,repository=session.repository,target=session.target,
                              base=session.branch,prs=session.prs,choices=specification["choices"],
                              branch="codex/grid-merge-"+identity,phase="idle",running=False,error="",head="",url="")
        else:
            raise ValueError("发布记录不存在，请重新预览")

    def save(self, **values):
        with self.guard:
            self.state.update(values)
            temp=self.state_file.with_suffix(".tmp")
            temp.write_bytes(json_bytes(self.state))
            temp.replace(self.state_file)

    def status(self):
        with self.guard:
            return {"ok":True,**self.state}

    def start(self):
        with self.guard:
            if self.running or self.state.get("url"):
                return {"ok":True,**self.state}
            if self.session is None and not self.state.get("head"):
                raise ValueError("提交尚未完成且原会话已丢失，请重新预览同一组 PR")
            self.running=True
        self.save(running=True,phase="checking",error="")
        threading.Thread(target=self._run,name="grid-pr-publish",daemon=True).start()
        return self.status()

    def merge_to_base(self):
        """Explicit user action only. Respect GitHub checks and branch protection."""
        with self.guard:
            if self.running:
                raise ValueError("发布任务仍在运行，请等待完成")
            if not self.state.get("number") or not self.state.get("url"):
                raise ValueError("请先创建集成 PR，再合入目标分支")
            if self.state.get("merged"):
                return {"ok":True,**self.state}
            self.running=True
        self.save(running=True,phase="merging",error="",merge_error="")
        try:
            self.gh_path=shutil.which("gh")
            if not self.gh_path:raise ValueError("未找到 GitHub CLI，请先安装并登录")
            if self.review._repository_name()!=self.state["repository"]:
                raise ValueError("数据仓库已切换，请重新预览")
            endpoint=f"repos/{self.state['repository']}/pulls/{self.state['number']}"
            pr=json.loads(self.gh("api",endpoint))
            if (pr["head"]["sha"]!=self.state["head"] or pr["base"]["ref"]!=self.state["base"]
                    or pr["base"]["repo"]["full_name"]!=self.state["repository"]):
                raise ValueError("集成 PR 版本或目标已改变，请重新预览")
            if pr.get("merged"):
                self.save(phase="merged",merged=True,merge_sha=pr.get("merge_commit_sha"),pr_state="merged")
            else:
                if pr["state"]!="open" or pr.get("draft"):
                    raise ValueError("集成 PR 已关闭或仍是草稿，不能合入")
                self.fresh()
                if pr["base"]["sha"]!=self.state["target"]:
                    raise ValueError("目标分支已更新，请重新预览")
                if pr.get("mergeable") is False:
                    raise ValueError("GitHub 检测到合并冲突，请重新预览")
                payload=self.directory/"merge-request.json"
                payload.write_bytes(json_bytes({"sha":self.state["head"],"merge_method":"merge"}))
                result=json.loads(self.gh("api","--method","PUT",endpoint+"/merge",
                                          "-H","X-GitHub-Api-Version: 2022-11-28","--input",str(payload.resolve())))
                if not result.get("merged"):
                    raise ValueError(result.get("message") or "GitHub 尚未允许合并，请检查分支保护和检查结果")
                self.save(phase="merged",merged=True,merge_sha=result.get("sha"),pr_state="merged")
        except Exception as error:
            # Keep the successfully created PR visible and retryable. A lost
            # merge response is recovered by the GET above on the next click.
            self.save(phase="complete",merge_error=str(error))
        finally:
            self.save(running=False)
            with self.guard:self.running=False
        return self.status()

    def command_environment(self):
        return os.environ.copy()

    def _command(self, args, *, cwd=None, data=None, timeout=180):
        env=dict(self.command_environment(),GIT_TERMINAL_PROMPT="0",GH_PROMPT_DISABLED="1",GIT_LFS_SKIP_SMUDGE="1")
        # An enclosing Git command must never redirect our isolated index/repo.
        for key in ("GIT_DIR","GIT_WORK_TREE","GIT_INDEX_FILE","GIT_OBJECT_DIRECTORY","GIT_ALTERNATE_OBJECT_DIRECTORIES"):
            env.pop(key,None)
        try:
            result=subprocess.run(args,cwd=cwd,input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                  env=env,timeout=timeout,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        except FileNotFoundError as error:
            raise ValueError("发布需要 Git、Git LFS 和已登录的 GitHub CLI") from error
        except subprocess.TimeoutExpired as error:
            raise ValueError("操作超时；可重试以检查远端结果并继续") from error
        if result.returncode:
            detail=result.stderr.decode("utf-8","replace")[-1600:] or "命令执行失败"
            if env.get("GH_TOKEN"):detail=detail.replace(env["GH_TOKEN"],"[已隐藏凭证]")
            raise ValueError(detail)
        return result.stdout

    def gh(self, *args):
        return self._command([self.gh_path,*args],cwd=self.directory,timeout=120)

    def git(self,*args,data=None,timeout=180):
        return self._command(["git","-C",str(self.work),"-c","core.hooksPath="+str(self.empty_hooks),
                              *args],data=data,timeout=timeout)

    def write_tree(self, changes, parent, original):
        self.git("read-tree",parent)
        for path in sorted(changes,key=lambda p:(not p.endswith(".gitattributes"),p)):
            safe_path(path)
            content=changes[path]
            if content is None:
                self.git("update-index","--force-remove","--",path)
                continue
            attributes=self.git("check-attr","--cached","-z","filter","--",path).split(b"\0")
            filter_name=attributes[2].decode()
            if filter_name=="lfs":
                content=self.git("lfs","clean","--",path,data=content)
                if not content.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
                    raise ValueError("Git LFS 未生成有效指针："+path)
            elif filter_name not in ("unspecified","unset"):
                raise ValueError("不支持自动执行文件过滤器："+filter_name)
            oid=self.git("hash-object","-w","--stdin",data=content).decode().strip()
            mode=original.get(path,("100644",))[0]
            if mode not in ("100644","100755"):raise ValueError("不支持该文件模式："+path)
            self.git("update-index","--add","--cacheinfo",mode,oid,path)
        return self.git("write-tree").decode().strip()

    def fresh(self):
        if self.review._repository_name()!=self.state["repository"]:
            raise ValueError("数据仓库已改变，请重新预览")
        for item in self.state["prs"]:
            pr=self.review._github(self.state["repository"],item["number"])
            if (pr["state"]!="open" or pr["head"]["sha"]!=item["head"]
                    or pr["base"]["sha"]!=self.state["target"] or pr["base"]["ref"]!=self.state["base"]):
                raise ValueError("PR 或目标分支已更新，请重新预览；已推送的集成分支会保留")

    def find_pr(self):
        items=json.loads(self.gh("pr","list","--repo",self.state["repository"],"--state","all",
                                "--head",self.state["branch"],"--base",self.state["base"],
                                "--json","url,number,headRefOid,body,state","--limit","100"))
        marker="<!-- nav-grid-integration:"+self.state["id"]+" -->"
        for item in items:
            if marker in item.get("body",""):
                if item["headRefOid"] != self.state["head"]:
                    raise ValueError("集成 PR 分支已被其他提交更新，请到 GitHub 检查")
                return item
        if items:
            raise ValueError("同名分支已有其他 PR，拒绝覆盖或重复创建")
        return None

    def _build(self):
        self.save(phase="validating")
        # Reuse export validation (including final endpoint validity) and its exact
        # finalized NPZ/JSON bytes. This never writes the editable data checkout.
        archive=self.session.export()
        self.save(export=str(archive.resolve()))
        self.work=self.directory / ("git-"+uuid.uuid4().hex)
        self.empty_hooks=self.directory / "empty-hooks";self.empty_hooks.mkdir(exist_ok=True)
        self.work.mkdir()
        self.git("init","-q")
        for key in ("user.name","user.email"):
            value=self.review._git("config","--get",key).decode().strip()
            if not value:raise ValueError("请先配置 Git user.name 和 user.email")
            self.git("config","--local",key,value)
        self.git("config","--local","credential.helper","")
        self.git("config","--local","--add","credential.helper","!"+shlex.quote(Path(self.gh_path).as_posix())+" auth git-credential")
        self.git("config","--local","lfs.storage",str((self.work/".git"/"lfs").resolve()))
        self.git("config","--local","lfs.url",f"https://github.com/{self.state['repository']}.git/info/lfs")
        self.git("remote","add","origin",f"https://github.com/{self.state['repository']}.git")
        self.git("fetch","--no-tags","--no-recurse-submodules",str(self.review.repository.resolve()),
                 self.state["target"],*(p["head"] for p in self.state["prs"]),timeout=300)
        self.save(phase="committing",work=str(self.work.resolve()))
        parent=self.state["target"]
        commits=[]
        for index,(pr,stage) in enumerate(zip(self.state["prs"],self.session.stages)):
            changes=self.session.result if index==len(self.session.stages)-1 else self.session.finalize_manifest(stage)
            self.git("read-tree",parent)
            # Cache attributes first, then explicitly clean only LFS files.
            # No checkout, hooks or arbitrary configured content filters run.
            paths=sorted(changes,key=lambda p:(not p.endswith(".gitattributes"),p))
            for path in paths:
                safe_path(path)
                content=changes[path]
                if content is None:
                    self.git("update-index","--force-remove","--",path)
                    continue
                attributes=self.git("check-attr","--cached","-z","filter","--",path).split(b"\0")
                filter_name=attributes[2].decode()
                if filter_name=="lfs":
                    content=self.git("lfs","clean","--",path,data=content)
                    if not content.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
                        raise ValueError("Git LFS 未生成有效指针："+path)
                elif filter_name not in ("unspecified","unset"):
                    raise ValueError("不支持自动执行文件过滤器："+filter_name)
                oid=self.git("hash-object","-w","--stdin",data=content).decode().strip()
                old=self.session.tree(pr["head"]).get(path) or self.session.tree(self.state["target"]).get(path)
                mode=old[0] if old else "100644"
                if mode not in ("100644","100755"):raise ValueError("不支持该文件模式："+path)
                self.git("update-index","--add","--cacheinfo",mode,oid,path)
            tree=self.git("write-tree").decode().strip()
            message=f"feat(data): 集成网格 PR #{pr['number']}\n\nSource-PR: {pr['number']}\nSource-Head: {pr['head']}\n"
            parent=self.git("commit-tree",tree,"-p",parent,"-p",pr["head"],data=message.encode()).decode().strip()
            commits.append(parent)
        self.git("update-ref","refs/heads/"+self.state["branch"],parent)
        self.save(head=parent,commits=commits)

    def _remote_head(self):
        response=self.git("ls-remote","--heads","origin","refs/heads/"+self.state["branch"]).decode().split()
        return response[0] if response else None

    def _push(self):
        existing=self._remote_head()
        if existing:
            if existing!=self.state["head"]:
                raise ValueError("远端同名分支内容不同，拒绝强制推送")
            return
        self.save(phase="lfs")
        # Fetch original PR histories too: a fork may contribute LFS objects not
        # previously stored by the destination repo. New clean objects are local.
        self.git("lfs","fetch","--all","origin",self.state["target"],
                 *(pr["head"] for pr in self.state["prs"]),timeout=900)
        self.git("lfs","push","--all","origin",self.state["branch"],timeout=900)
        self.fresh()
        self.save(phase="pushing")
        # Empty expected value means create-only. Even a concurrently created,
        # fast-forward-compatible branch cannot be silently advanced.
        self.git("push","--porcelain","--force-with-lease=refs/heads/"+self.state["branch"]+":",
                 "origin",self.state["head"]+":refs/heads/"+self.state["branch"],timeout=300)

    def _create_pr(self):
        self.save(phase="creating_pr")
        existing=self.find_pr()
        if existing:return existing
        self.fresh()
        title="feat(data): 集成网格修改 "+"、".join("#"+str(pr["number"]) for pr in self.state["prs"])
        body=(f"将以下网格修改按顺序三方合并到 `{self.state['base']}`，保留各 PR 的祖先关系。\n\n"
              +"\n".join(f"- #{p['number']} · `{p['head']}`" for p in self.state["prs"])
              +f"\n\n基准：`{self.state['target']}`\n\n"
              +f"已处理 {len(self.state['choices'])} 个冲突区域或元数据冲突；最终网格格式、清单与连接端点已检查。\n\n"
              +"请使用 **Create a merge commit** 合入，以保留原 PR 的祖先关系。原始 PR 未被自动关闭。\n\n"
              +"<!-- nav-grid-integration:"+self.state["id"]+" -->\n")
        payload=dict(title=title,body=body,head=self.state["branch"],base=self.state["base"])
        request=self.directory/"pull-request.json";request.write_bytes(json_bytes(payload))
        # Structured JSON on disk preserves literal newlines and never interpolates
        # a PR title/body through a shell.
        result=json.loads(self.gh("api","--method","POST",f"repos/{self.state['repository']}/pulls","--input",str(request.resolve())))
        return dict(url=result["html_url"],number=result["number"],state=result["state"])

    def _run(self):
        try:
            self.gh_path=shutil.which("gh")
            if not self.gh_path:raise ValueError("未找到 GitHub CLI，请安装并运行 gh auth login")
            self.gh("auth","status","--hostname","github.com")
            repository=json.loads(self.gh("api","repos/"+self.state["repository"]))
            if not repository.get("permissions",{}).get("push"):
                raise ValueError("当前 GitHub 账号没有数据仓库推送权限")
            if not repository.get("allow_merge_commit",False):
                raise ValueError("数据仓库需要启用 merge commit，才能保留原始 PR 祖先关系")
            if self.state.get("head"):
                self.work=Path(self.state["work"])
                self.empty_hooks=self.directory/"empty-hooks"
                existing=self.find_pr()
                if existing:
                    self.save(phase="complete",url=existing["url"],number=existing["number"],pr_state=existing["state"])
                    return
            self.fresh()
            if not self.state.get("head"):self._build()
            self._push()
            self.save(pushed=True)
            pr=self._create_pr()
            self.save(phase="complete",url=pr["url"],number=pr["number"],pr_state=pr["state"])
        except Exception as error:
            self.save(phase="failed",error=str(error))
        finally:
            self.save(running=False)
            with self.guard:self.running=False

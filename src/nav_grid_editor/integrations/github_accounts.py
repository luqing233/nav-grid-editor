"""Use one explicitly selected local gh account without switching global login."""
import json
import os
import re
import shutil
import subprocess


def login_environment():
    env = dict(os.environ, GH_PROMPT_DISABLED="1", GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        env.pop(key, None)
    return env


def gh_command(*args, env=None, secret=False):
    gh = shutil.which("gh")
    if not gh:
        raise ValueError("请先安装 GitHub CLI，并执行 gh auth login --hostname github.com --web")
    try:
        result = subprocess.run([gh, *args], env=env or login_environment(), capture_output=True,
                                timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise ValueError("GitHub 请求超时，请检查网络后重试") from None
    if result.returncode:
        if secret:
            raise ValueError("无法读取所选账号的登录凭证，请在终端重新登录该账号")
        detail = result.stderr.decode("utf-8", "replace")[-1200:]
        if env and env.get("GH_TOKEN"):
            detail = detail.replace(env["GH_TOKEN"], "[已隐藏凭证]")
        raise ValueError("GitHub 请求失败：" + detail)
    return result.stdout


def list_accounts():
    data = json.loads(gh_command("auth", "status", "--hostname", "github.com", "--json", "hosts"))
    # Never return gh's raw response: it contains credential diagnostics/fields.
    return {"ok": True, "accounts": [
        dict(login=a["login"], active=bool(a.get("active")), available=a.get("state")=="success")
        for a in data.get("hosts", {}).get("github.com", []) if a.get("login")
    ]}


class GitHubAccount:
    def __init__(self, login):
        if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login):
            raise ValueError("请选择已登录的 GitHub 账号")
        self.login = login
        # stdout stays in memory; never place this token in a URL, file or response.
        self._token = gh_command("auth", "token", "--hostname", "github.com", "--user", login, secret=True).decode().strip()
        if not self._token:
            raise ValueError("所选账号没有可用凭证，请重新登录")
        self.user = self.api("user")
        if self.user.get("login", "").lower() != login.lower():
            raise ValueError("登录凭证与所选账号不一致，已停止发布")

    def environment(self):
        return dict(login_environment(), GH_TOKEN=self._token)

    def api(self, path):
        return json.loads(gh_command("api", path, env=self.environment()))

    def redact(self, text):
        return text.replace(self._token, "[已隐藏凭证]")

# SPDX-License-Identifier: Apache-2.0
"""把本地提交推到 GitHub，不挑环境。

为什么需要这个脚本，而不是直接 `git push`：

1. **证书**。有代理（企业网关、加速器、本工具链自带的出网代理）在做 TLS
   中间人时，git 会因为「找不到签发者」或「查不到吊销列表」失败，而 curl /
   httpx 却正常——因为它们读的是另一份 CA。把某个人的 CA 路径写进
   `.git/config`，换台机器就废；所以这里运行时现找，找到就用，
   找不到就让 git 用系统默认。
2. **网络**。有的环境只放行 api.github.com，挡掉 github.com:443，
   这时 git 的传输层直接不可用。脚本会自动退回 Git Data API，
   效果等价：上传 blob → 建树 → 建提交 → 移动分支。

令牌只从命令行参数或环境变量读，不写进任何文件。

用法：
    python scripts/publish.py --doctor                 # 只诊断，不改任何东西
    python scripts/publish.py                          # 推当前 main
    python scripts/publish.py -m "提交说明"            # 先提交工作区再推
    STEWARD_GH_TOKEN=... python scripts/publish.py     # 令牌走环境变量更安全
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_ROOT = "https://api.github.com"

#: 各家代理/工具链把 CA 包放的位置。按可信度排，先看显式声明的。
CA_ENV_VARS = ("SSL_CERT_FILE", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS")
CA_SYSTEM_PATHS = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian / Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL / CentOS
    "/etc/ssl/cert.pem",  # macOS / Alpine
    "C:/ProgramData/certifi/cacert.pem",
)


# ---------------------------------------------------------------- 小工具


def run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(cwd or ROOT), capture_output=True, text=True, errors="replace")


def git(*args: str) -> str:
    proc = run(["git", *args])
    if proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：\n{proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def token_from(args: argparse.Namespace) -> str:
    token = args.token or os.environ.get("STEWARD_GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        raise SystemExit(
            "没有令牌。用 STEWARD_GH_TOKEN 环境变量，或 --token（不推荐：会留在 shell 历史里）。"
        )
    return token.strip()


def repo_slug() -> str:
    """从 origin 的 URL 里抽 owner/name。"""
    url = git("remote", "get-url", "origin").strip()
    slug = url.split("github.com", 1)[-1].lstrip(":/").removesuffix(".git")
    if slug.count("/") != 1:
        raise SystemExit(f"看不懂的远端地址：{url}")
    return slug


# ---------------------------------------------------------------- 证书


def ca_candidates() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for name in CA_ENV_VARS:
        value = os.environ.get(name)
        if value and Path(value).is_file():
            found.append((f"env {name}", value))
    for path in CA_SYSTEM_PATHS:
        if Path(path).is_file():
            found.append(("系统", path))
    try:
        import certifi

        found.append(("certifi", certifi.where()))
    except Exception:  # noqa: BLE001 - 没装 certifi 很正常
        pass
    return found


def git_tls_args() -> list[str]:
    """给 git 找一份能用的 CA，找不到就交给系统默认信任链。"""
    args = ["-c", "http.sslBackend=openssl"] if os.name == "nt" else []
    for _, path in ca_candidates():
        args += ["-c", f"http.sslCAInfo={path}"]
        break
    return args


async def resolve_verify() -> tuple[str | bool, str]:
    """挑一份真能连通 GitHub 的 CA，返回 (verify, 说明)。

    不能只按「文件存在」挑：有代理在做中间人时，系统信任链过不去，而代理
    自己的 CA 包能过；反过来在某些机器上代理 CA 已经过期、系统链才是对的。
    所以真的连一次 api.github.com，谁通用谁。
    """
    import httpx

    errors: list[str] = []
    for label, path in ca_candidates():
        try:
            async with httpx.AsyncClient(verify=path, timeout=10, follow_redirects=False) as client:
                await client.get(f"{API_ROOT}/")
            return path, f"{label} ({path})"
        except Exception as exc:  # noqa: BLE001 - 逐个试，失败就换下一个
            errors.append(f"{label}: {type(exc).__name__}")
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            await client.get(f"{API_ROOT}/")
        return True, "系统默认信任链"
    except Exception as exc:  # noqa: BLE001
        errors.append(f"系统默认: {type(exc).__name__}")
    return False, "；".join(errors)


# ---------------------------------------------------------------- git 路线


def git_push(token: str, branch: str, dry_run: bool = False) -> tuple[bool, str]:
    """用 git 推。

    凭据走 URL（`x-access-token:<PAT>@`），不写进 .git/config，也不落到磁盘：
    这是 git-over-HTTPS 认的形式。`Authorization: bearer` 只对 api.github.com
    有效，拿来推 git 会得到一句 "Authentication failed"，很容易被误判成令牌失效。
    """
    url = f"https://x-access-token:{token}@github.com/{repo_slug()}.git"
    args = [
        "git",
        *git_tls_args(),
        "-c",
        "credential.helper=",  # 别弹窗，也别去翻 Windows 凭据管理器
        "push",
        url,
        branch,
    ]
    if dry_run:
        args.append("--dry-run")
    proc = subprocess.run(
        args,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        errors="replace",
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    detail = (proc.stderr or proc.stdout).strip().replace(token, "<token>")
    return proc.returncode == 0, detail[-800:]


# ---------------------------------------------------------------- API 路线


async def api_push(token: str, branch: str, message: str | None) -> tuple[bool, str]:
    """用 Git Data API 把本地 HEAD 原样搬上去。

    blob 是内容寻址的，本地对象的 SHA 与 GitHub 算出来的必然一致，
    所以这条路的产物和 git push 等价，而且可以用哈希对账。
    """
    import httpx

    verify, how = await resolve_verify()
    if verify is False:
        return False, f"连不上 GitHub，证书一个都没过：{how}"
    slug = repo_slug()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    api = f"{API_ROOT}/repos/{slug}"

    entries: list[dict] = []
    for line in git("ls-tree", "-r", "HEAD").splitlines():
        meta, path = line.split("\t", 1)
        mode, typ, sha = meta.split()
        entries.append({"mode": mode, "type": typ, "sha": sha, "path": path})

    async with httpx.AsyncClient(timeout=90, headers=headers, verify=verify) as client:
        # 仓库可能是空的：先播一个提交，否则 GitHub 拒绝收 blob。
        head = await client.get(f"{api}/git/ref/heads/{branch}")
        parent = None
        if head.status_code == 404:
            readme = next((e for e in entries if e["path"] == "README.md"), None)
            if readme is None:
                return False, "空仓库且没有 README.md，无法播种"
            content = subprocess.run(
                ["git", "cat-file", "blob", readme["sha"]], capture_output=True, check=True
            ).stdout
            seed = await client.put(
                f"{api}/contents/README.md",
                json={"message": "chore: 初始化仓库", "content": base64.b64encode(content).decode()},
            )
            if seed.status_code not in (200, 201):
                return False, f"播种失败 {seed.status_code}: {seed.text[:200]}"
            parent = seed.json()["commit"]["sha"]
        elif head.status_code == 200:
            parent = head.json()["object"]["sha"]
        else:
            return False, f"读分支失败 {head.status_code}: {head.text[:200]}"

        sem = asyncio.Semaphore(6)
        mismatched: list[str] = []

        async def put_blob(entry: dict) -> str | None:
            raw = subprocess.run(
                ["git", "cat-file", "blob", entry["sha"]], capture_output=True, check=True
            ).stdout
            async with sem:
                for attempt in range(3):
                    try:
                        r = await client.post(
                            f"{api}/git/blobs",
                            json={"content": base64.b64encode(raw).decode(), "encoding": "base64"},
                        )
                        if r.status_code in (200, 201):
                            if r.json()["sha"] != entry["sha"]:
                                mismatched.append(entry["path"])
                            return r.json()["sha"]
                    except httpx.HTTPError:
                        await asyncio.sleep(1 + attempt)
            return None

        blobs = await asyncio.gather(*(put_blob(e) for e in entries))
        if any(b is None for b in blobs):
            return False, "有 blob 上传失败"

        tree = [{"path": e["path"], "mode": e["mode"], "type": "blob", "sha": b} for e, b in zip(entries, blobs)]
        r = await client.post(f"{api}/git/trees", json={"tree": tree})
        if r.status_code not in (200, 201):
            return False, f"建树失败 {r.status_code}: {r.text[:200]}"
        tree_sha = r.json()["sha"]
        local_tree = git("rev-parse", "HEAD^{tree}").strip()
        if tree_sha != local_tree:
            # 中文文件名在部分实现里按有符号字节排序，会让树哈希对不上；
            # 内容仍然是对的，但 clone 后可能读不动，所以明说。
            return False, f"树哈希不一致（本地 {local_tree[:10]} / 远端 {tree_sha[:10]}），文件名里可能有非 ASCII 字符"

        msg = message or git("log", "-1", "--format=%B").strip()
        r = await client.post(
            f"{api}/git/commits", json={"message": msg, "tree": tree_sha, "parents": [parent] if parent else []}
        )
        if r.status_code not in (200, 201):
            return False, f"建提交失败 {r.status_code}: {r.text[:200]}"
        commit = r.json()["sha"]

        r = await client.patch(f"{api}/git/refs/heads/{branch}", json={"sha": commit, "force": False})
        if r.status_code == 422:  # 分支还不存在
            r = await client.post(f"{api}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": commit})
        if r.status_code not in (200, 201):
            return False, f"移动分支失败 {r.status_code}: {r.text[:200]}"

        note = f"共 {len(entries)} 个文件，树 {tree_sha[:10]}"
        if mismatched:
            note += f"；{len(mismatched)} 个 blob 哈希与本地不同（内容可能被服务端改写）"
        return True, note


# ---------------------------------------------------------------- tag 与 release


def git_push_tag(token: str, tag: str) -> tuple[bool, str]:
    """推一个 tag。和推分支同一套凭据与 CA 处理，只是 ref 换了。"""
    url = f"https://x-access-token:{token}@github.com/{repo_slug()}.git"
    proc = subprocess.run(
        ["git", *git_tls_args(), "-c", "credential.helper=", "push", url, f"refs/tags/{tag}"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        errors="replace",
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    detail = (proc.stderr or proc.stdout).strip().replace(token, "<token>")
    return proc.returncode == 0, detail[-500:]


def tag_body(tag: str) -> str:
    """annotated tag 的说明直接用这次提交的标题行，别写空。"""
    subject = git("log", "-1", "--format=%s").strip()
    return f"{tag}\n\n{subject}" if subject else tag


async def create_release(token: str, tag: str, title: str, notes: str, prerelease: bool, draft: bool) -> tuple[bool, str]:
    """建一个 GitHub Release。

    已经存在同名 release 时返回失败并说明——GitHub 不让你改历史，
    要改只能先删再建，那应该是人的决定，不该由脚本猜。
    """
    import httpx

    verify, how = await resolve_verify()
    if verify is False:
        return False, f"连不上 GitHub，证书一个都没过：{how}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "tag_name": tag,
        "name": title or tag,
        "body": notes,
        "draft": draft,
        "prerelease": prerelease,
    }
    async with httpx.AsyncClient(timeout=45, headers=headers, verify=verify) as client:
        r = await client.post(f"{API_ROOT}/repos/{repo_slug()}/releases", json=payload)
        if r.status_code == 422 and "already_exists" in r.text:
            return False, f"{tag} 已经有 Release 了，要改先手动删掉旧的"
        if r.status_code not in (200, 201):
            return False, f"{r.status_code}: {r.text[:300]}"
        data = r.json()
        return True, f"{data['html_url']}（{data['name']}，{'草稿' if draft else '已发布'}）"


# ---------------------------------------------------------------- 诊断


async def doctor(token: str | None) -> int:
    import httpx

    print("== git ==")
    v = run(["git", "--version"])
    print(f"  {v.stdout.strip() or '未找到 git'}")
    print(f"  远端  {git('remote', 'get-url', 'origin').strip() if run(['git','remote','get-url','origin']).returncode == 0 else '（没有 origin）'}")
    print(f"  分支  {git('rev-parse', '--abbrev-ref', 'HEAD').strip()}")
    print(f"  提交  {git('rev-parse', '--short', 'HEAD').strip()}")
    dirty = len([l for l in git("status", "--porcelain").splitlines() if l.strip()])
    print(f"  工作区 {'干净' if dirty == 0 else f'{dirty} 个改动'}")

    print("\n== 证书 ==")
    cands = ca_candidates()
    if not cands:
        print("  没找到额外的 CA 包，会用 git 自带的信任链")
    for label, path in cands:
        print(f"  {label:<22} {path}")

    print("\n== 连通性 ==")
    verify, how = await resolve_verify()
    print(f"  用哪份 CA：{how}")
    if verify is False:
        print("  连不上 GitHub —— 先确认网络/代理，再谈推送")
    async with httpx.AsyncClient(timeout=12, follow_redirects=False, verify=verify) as client:
        for url in ("https://github.com", "https://api.github.com"):
            try:
                r = await client.get(url)
                print(f"  {url:<26} HTTP {r.status_code}")
            except Exception as exc:  # noqa: BLE001
                print(f"  {url:<26} 失败 {type(exc).__name__}: {exc}")

    print("\n== 令牌 ==")
    if not token:
        print("  未提供（只诊断到这一步）")
        return 0
    async with httpx.AsyncClient(timeout=15, verify=verify, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}) as client:
        r = await client.get(f"{API_ROOT}/user")
        if r.status_code == 200:
            me = r.json()
            print(f"  有效：{me.get('login')}（{me.get('type')}）")
            repo = await client.get(f"{API_ROOT}/repos/{repo_slug()}")
            print(f"  仓库 {'可读' if repo.status_code == 200 else f'读不到（{repo.status_code}）'}")
        else:
            print(f"  无效或权限不足：HTTP {r.status_code} {r.text[:120]}")
            return 1
    ok, detail = git_push(token, git("rev-parse", "--abbrev-ref", "HEAD").strip(), dry_run=True)
    print(f"\n  git push 预演：{'通过' if ok else '失败'}")
    if not ok:
        print(f"    {detail.splitlines()[-1] if detail else ''}")
    return 0


# ---------------------------------------------------------------- 主流程


async def main_async(args: argparse.Namespace) -> int:
    token = token_from(args)

    if args.doctor:
        return await doctor(token)

    branch = args.branch or git("rev-parse", "--abbrev-ref", "HEAD").strip()

    if args.release and not args.tag:
        args.tag = args.release

    if args.message:
        run(["git", "add", "-A"])
        commit = run(["git", "commit", "-m", args.message])
        print(commit.stdout.strip() or commit.stderr.strip())
        if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr):
            return 1

    # 先把 TLS 试通：能走 git 就走 git，它顺带处理了 force-with-lease、hooks 这些语义。
    ok, detail = git_push(token, branch)
    if ok:
        print(f"已用 git push 推送到 {branch}")
    else:
        reason = detail.splitlines()[-1] if detail else "未知原因"
        print(f"git push 不通（{reason}），改用 Git Data API")
        ok, detail = await api_push(token, branch, args.message)
        if not ok:
            print(f"API 推送失败：{detail}")
            return 1
        print(f"已用 API 推送：{detail}")
        print("提示：本地 HEAD 与远端提交对象可能不同（服务端会重写 author 字段），但树一致。")

    if not args.tag:
        return 0

    # 打 tag 之前先确认它指向的提交已经在远端，否则 release 会指向一个孤儿
    remote = run(["git", *git_tls_args(), "ls-remote", "--tags", "origin", f"refs/tags/{args.tag}"])
    if remote.returncode == 0 and args.tag not in remote.stdout:
        ok, detail = git_push_tag(token, args.tag)
        print(f"{'已推送 tag' if ok else 'tag 推送失败'} {args.tag}" + ("" if ok else f"：{detail}"))
        if not ok:
            return 1
    else:
        print(f"tag {args.tag} 已在远端")

    if not args.release:
        return 0

    notes = ""
    if args.notes_file:
        notes = Path(args.notes_file).read_text(encoding="utf-8")
    ok, detail = await create_release(token, args.release, args.title, notes, args.prerelease, args.draft)
    print(f"{'已创建 Release' if ok else 'Release 创建失败'}：{detail}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="推送到 GitHub：git push 优先，走不通就换 Git Data API；可选打 tag 与建 Release"
    )
    ap.add_argument("-m", "--message", default="", help="先提交工作区改动，再推送")
    ap.add_argument("-t", "--token", default="", help="GitHub 令牌（更推荐 STEWARD_GH_TOKEN 环境变量）")
    ap.add_argument("-b", "--branch", default="", help="分支名，默认当前分支")
    ap.add_argument("--doctor", action="store_true", help="只诊断：git、证书、连通性、令牌、推送预演")
    ap.add_argument("--tag", default="", help="推送这个 tag（本地需已存在）")
    ap.add_argument("--release", default="", help="为这个 tag 建 Release（同时会推 tag）")
    ap.add_argument("--title", default="", help="Release 标题，默认用 tag 名")
    ap.add_argument("--notes-file", default="", help="Release 正文所用的 Markdown 文件")
    ap.add_argument("--prerelease", action="store_true", help="标为预发布")
    ap.add_argument("--draft", action="store_true", help="存为草稿，不对外可见")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())

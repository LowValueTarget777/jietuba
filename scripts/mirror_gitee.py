"""Mirror verified public Jietuba releases to Gitee; preview unless --apply is set."""

import argparse
import contextlib
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SOURCE = "1003129155/jietuba"
MARKER = f"<!-- jietuba-mirror:{SOURCE} -->"
FILE_LIMIT = 100_000_000
JSON_LIMIT = 4_000_000
CHECKSUM_LIMIT = 64_000
TAG_PATTERN = r"release-([0-9]+)\.([0-9]+)\.([0-9]+)"


def log(message):
    print(message, flush=True)


class MirrorError(Exception):
    pass


def version(tag):
    match = re.fullmatch(TAG_PATTERN, tag)
    if not match:
        raise MirrorError("只接受 release-X.Y.Z 格式的稳定版本")
    return tuple(map(int, match.groups()))


def package_names(tag):
    version(tag)
    return [f"jietuba_{edition}-{tag}-{arch}.zip"
            for edition in ("lite", "pp") for arch in ("x64", "arm64")]


def managed_names(tag):
    return {*package_names(tag), "SHA256SUMS.txt"}


def checksums(content, tag):
    result = {}
    for line in content.decode("utf-8-sig").splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([a-fA-F0-9]{64}) [ *](.+)", line)
        if not match or match[2] not in package_names(tag) or match[2] in result:
            raise MirrorError("SHA256SUMS.txt 包含未知路径、重复项或无效哈希")
        result[match[2]] = match[1].lower()
    if set(result) != set(package_names(tag)):
        raise MirrorError("SHA256SUMS.txt 必须包含完整版/轻量版的 x64/ARM64 四个包")
    return result


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # API 携带令牌，不允许重定向；公开附件允许 CDN 跳转但不能降级到 HTTP。
        if req.has_header("Authorization") or urllib.parse.urlsplit(newurl).scheme != "https":
            raise MirrorError("拒绝携带凭据或非 HTTPS 的重定向")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self):
        self.opener = urllib.request.build_opener(SafeRedirect())

    def transfer(self, url, dest, size, upload=None):
        """大文件交给 curl 流式传输，总时限覆盖连接、发送和接收全过程。"""
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise MirrorError("传输地址必须为无凭据的 HTTPS 地址")
        if upload is not None and parsed.hostname != "gitee.com":
            raise MirrorError("只允许向 Gitee 上传附件")
        curl = shutil.which("curl.exe" if os.name == "nt" else "curl")
        if not curl:
            raise MirrorError("未找到 curl，请安装后重试")
        # 实测跨境上传约 20 KB/s；按更保守的 16 KB/s 给大包预算，仍有明确总时限。
        limit = max(900, (size + 15_999) // 16_000 + 120) if upload is not None else 300
        label = ("上传 " + upload.name) if upload is not None else ("下载 " + dest.name)
        args = [curl, "--disable", "--proto", "=https", "--proto-redir", "=https",
                "--connect-timeout", "15", "--max-time", str(limit),
                "--speed-limit", "1024", "--speed-time", "60", "--progress-bar",
                "--max-filesize", str(JSON_LIMIT if upload is not None else size),
                "--user-agent", "jietuba-release-mirror", "--output", str(dest),
                "--write-out", "%{http_code}", "--config", "-"]
        config = ""
        if upload is None:
            args += ["--location", "--max-redirs", "5"]
        else:
            token = os.environ.get("GITEE_TOKEN", "")
            if not token or any(c in token for c in "\r\n\0"):
                raise MirrorError("GITEE_TOKEN 缺失或格式无效")
            # 凭据只经标准输入传给 curl，不出现在命令行和进度日志中。
            config = "header = " + json.dumps("Authorization: Bearer " + token) + "\n"
            form_path = upload.resolve().as_posix().replace("\\", "\\\\").replace('"', '\\"')
            args += ["--header", "Expect:", "--form", f'file=@"{form_path}";filename="{upload.name}"']
        args += [url]
        started = time.monotonic()
        log(f"{label} 开始，大小 {size / 1_000_000:.2f} MB，总时限 {limit} 秒")
        with tempfile.TemporaryDirectory(prefix="mirror-progress-") as temp:
            progress_path = Path(temp) / "progress.txt"
            progress = progress_path.open("wb")
            try:
                proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=progress)
            except OSError:
                progress.close()
                raise MirrorError(f"{label} 无法启动 curl") from None
            try:
                proc.stdin.write(config.encode())
                proc.stdin.close()
                while True:
                    elapsed = time.monotonic() - started
                    try:
                        proc.wait(timeout=min(15, max(0.1, limit + 10 - elapsed)))
                        break
                    except subprocess.TimeoutExpired:
                        if time.monotonic() - started >= limit + 10:
                            raise MirrorError(f"{label} 超过总时限") from None
                        if upload is None:
                            count = dest.stat().st_size if dest.exists() else 0
                            log(f"{label} 已接收 {count / 1_000_000:.2f}/{size / 1_000_000:.2f} MB，耗时 {time.monotonic() - started:.0f} 秒")
                        else:
                            with progress_path.open("rb") as reader:
                                reader.seek(max(0, progress_path.stat().st_size - 4096))
                                percent = re.findall(rb"([0-9]+\.[0-9]+)%", reader.read())
                            value = percent[-1].decode() + "%" if percent else "等待传输或服务器响应"
                            log(f"{label} {value}，耗时 {time.monotonic() - started:.0f} 秒")
                status = proc.stdout.read(64).decode("ascii", errors="replace").strip()
                if proc.returncode or not re.fullmatch(r"2[0-9]{2}", status):
                    raise MirrorError(f"{label} 失败（curl={proc.returncode}, HTTP={status[:3]}）；下次运行会回读附件后续传")
            except OSError:
                raise MirrorError(f"{label} 与 curl 通信失败") from None
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                with contextlib.suppress(OSError):
                    proc.stdin.close()
                proc.stdout.close()
                progress.close()
        log(f"{label} 完成，耗时 {time.monotonic() - started:.1f} 秒")

    def open(self, method, url, data=None, headers=None):
        if urllib.parse.urlsplit(url).scheme != "https":
            raise MirrorError("只允许 HTTPS")
        request = urllib.request.Request(url, data=data, method=method,
                                         headers={"User-Agent": "jietuba-release-mirror", **(headers or {})})
        return self.opener.open(request, timeout=30)

    def api(self, method, url, data=None, headers=None):
        host = urllib.parse.urlsplit(url).hostname
        token = os.environ.get("GH_TOKEN" if host == "api.github.com" else "GITEE_TOKEN", "")
        headers = dict(headers or {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        # 写操作不盲目重试；若响应丢失，下一轮通过回读决定是否续传。
        for attempt in range(3 if method == "GET" else 1):
            try:
                with self.open(method, url, data, headers) as response:
                    body = response.read(JSON_LIMIT + 1)
                    if len(body) > JSON_LIMIT:
                        raise MirrorError("API 响应过大")
                    return json.loads(body) if body else None
            except urllib.error.HTTPError as exc:
                if method == "GET" and exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                # 不输出响应正文或完整 URL，避免服务端错误包含令牌。
                raise MirrorError(f"{host}: {method} HTTP {exc.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
                if method == "GET" and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                reason = type(getattr(exc, "reason", exc)).__name__
                raise MirrorError(f"{host}: {method} 网络请求失败或超时（{reason}）") from None
        raise MirrorError("API 请求失败")

    def pages(self, url):
        result, seen = [], set()
        for page in range(1, 101):
            items = self.api("GET", f"{url}?page={page}&per_page=100")
            if not isinstance(items, list):
                raise MirrorError("分页 API 未返回列表")
            for item in items:
                if item["id"] in seen:
                    raise MirrorError("分页返回重复记录，停止以免遗漏容量")
                seen.add(item["id"])
                result.append(item)
            if len(items) < 100:
                return result
        raise MirrorError("记录数量超过安全分页上限")

    def download(self, url, dest, size, digest=None):
        partial = dest.with_name(dest.name + ".part")
        for attempt in range(2):
            try:
                self.transfer(url, partial, size)
                with partial.open("rb") as source:
                    actual = hashlib.file_digest(source, "sha256").hexdigest()
                if partial.stat().st_size != size or (digest and digest != actual):
                    raise MirrorError(f"附件大小或 SHA-256 校验失败: {dest.name}")
                partial.replace(dest)
                return actual
            except (MirrorError, OSError):
                if attempt == 1:
                    raise
                log(f"下载 {dest.name} 未完成，准备第 {attempt + 2}/2 次尝试")
                time.sleep(2 ** attempt)
            finally:
                partial.unlink(missing_ok=True)
        raise MirrorError("附件下载失败")

    def upload(self, url, path):
        with tempfile.TemporaryDirectory(prefix="mirror-upload-") as temp:
            # POST 不盲目重试；响应丢失时，由调用方回读、下载校验来决定是否已成功。
            self.transfer(url, Path(temp) / "response.json", path.stat().st_size, upload=path)


def source_release(client, folder, download=True):
    release = client.api("GET", f"https://api.github.com/repos/{SOURCE}/releases/latest")
    tag = release["tag_name"]
    version(tag)
    if release.get("draft") or release.get("prerelease") or not release.get("published_at"):
        raise MirrorError("源版本尚未公开或不是稳定版")
    required = managed_names(tag)
    assets = {}
    # GitHub assets 独立分页，避免把缺失附件误当成完整版本。
    for asset in client.pages(f"https://api.github.com/repos/{SOURCE}/releases/{release['id']}/assets"):
        name = asset["name"]
        if name not in required:
            continue
        if name in assets or asset.get("state") != "uploaded":
            raise MirrorError("源版本附件重复或尚未上传完成")
        limit = CHECKSUM_LIMIT if name == "SHA256SUMS.txt" else FILE_LIMIT
        if not isinstance(asset["size"], int) or not 0 < asset["size"] <= limit:
            raise MirrorError(f"附件超过大小限制: {name}")
        assets[name] = asset
    if set(assets) != required:
        raise MirrorError("源版本的四个 ZIP 或 SHA256SUMS.txt 不完整；本次不修改镜像")

    print(f"核验上游版本: {tag}", flush=True)

    manifest = assets["SHA256SUMS.txt"]
    digest = client.download(manifest["browser_download_url"], folder / manifest["name"], manifest["size"])
    hashes = checksums((folder / manifest["name"]).read_bytes(), tag)
    hashes[manifest["name"]] = digest
    for name, asset in assets.items():
        api_digest = asset.get("digest")
        if api_digest and api_digest != "sha256:" + hashes[name]:
            raise MirrorError(f"GitHub 附件哈希与校验清单不一致: {name}")
        if download and name != manifest["name"]:
            client.download(asset["browser_download_url"], folder / name, asset["size"], hashes[name])
        asset["sha256"] = hashes[name]
    return release, assets


def verification_receipt(expected, files, partial=False):
    """记录已核验的附件身份；删除重传会改变 ID，触发下一次完整校验。"""
    selected = [a for a in files if a["name"] in expected]
    names = {a["name"] for a in selected}
    if not selected or len(names) != len(selected) or (not partial and names != set(expected)):
        return None
    state = []
    for asset in sorted(selected, key=lambda a: a["name"]):
        source = expected[asset["name"]]
        if asset["size"] != source["size"]:
            return None
        state.append([asset["name"], source["id"], source["size"], source["sha256"],
                      source.get("updated_at"), asset["id"], asset["size"]])
    digest = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
    return f"<!-- jietuba-mirror-verified:{digest} -->"


def inventory(client, base):
    releases = client.pages(base + "/releases")
    for release in releases:
        archives = {base.replace("/api/v5/repos/", "/") + "/archive/refs/tags/" + release["tag_name"] + suffix
                    for suffix in (".zip", ".tar.gz")}
        assets = release.get("assets")
        # 已清空附件的历史版本只剩自动源码链接，避免每小时逐版再请求空附件列表。
        if isinstance(assets, list) and all(a.get("browser_download_url") in archives for a in assets):
            release["files"] = []
            continue
        # Release.assets 混入自动生成的源码归档，也没有 size/id，不能用于容量和删除。
        release["files"] = client.pages(f"{base}/releases/{release['id']}/attach_files")
        for asset in release["files"]:
            if not isinstance(asset.get("size"), int) or asset["size"] < 0:
                raise MirrorError("镜像附件大小未知，不能安全计算容量")
    return releases


def owned_files(release):
    if MARKER not in (release.get("body") or "") or not re.fullmatch(TAG_PATTERN, release["tag_name"]):
        return []
    allowed = managed_names(release["tag_name"])
    return [asset for asset in release["files"] if asset["name"] in allowed]


def retention_plan(releases, tag, incoming, keep, budget):
    if not 1 <= keep <= 20 or not 0 < budget <= 800_000_000:
        raise MirrorError("保留数量须为 1–20，发行附件预算须为 1–800 MB")
    total = sum(a["size"] for r in releases for a in r["files"]) + incoming
    managed = [r for r in releases if owned_files(r) and r["tag_name"] != tag]
    managed.sort(key=lambda r: version(r["tag_name"]))
    # 已被新版取代的半包单独清理，不挤占完整稳定版的保留名额。
    removed = [r for r in managed if r.get("prerelease")]
    total -= sum(a["size"] for r in removed for a in owned_files(r))
    stable = [r for r in managed if not r.get("prerelease")]
    count = len(stable) + 1
    for release in stable:
        if count <= keep and total <= budget:
            break
        removed.append(release)
        count -= 1
        total -= sum(a["size"] for a in owned_files(release))
    if total > budget:
        raise MirrorError("新版和受保护/未托管附件超过预算；请检查其他附件或调整镜像范围")
    return removed


def verify_target(client, release, expected, folder):
    seen = set()
    for asset in release["files"]:
        name = asset["name"]
        if name not in expected:
            continue
        limit = CHECKSUM_LIMIT if name == "SHA256SUMS.txt" else FILE_LIMIT
        if name in seen or asset["size"] != expected[name]["size"] or not 0 < asset["size"] <= limit:
            raise MirrorError(f"镜像存在重复或大小冲突的附件: {name}")
        seen.add(name)
        client.download(asset["browser_download_url"], folder / ("mirror-" + name),
                        asset["size"], expected[name]["sha256"])
    return seen


def release_body(source):
    return (f"{MARKER}\n社区下载镜像，附件与上游一致。\n\n"
            f"上游发行页：https://github.com/{SOURCE}/releases/tag/{source['tag_name']}\n\n"
            "自动生成的 Source code 仅为镜像仓库快照；源码请访问上游。\n\n"
            + (source.get("body") or ""))


def run(client, target, keep, budget, apply, folder):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", target):
        raise MirrorError("目标仓库须为 owner/repo")
    base = f"https://gitee.com/api/v5/repos/{target}"
    repo = client.api("GET", base)
    if not repo.get("public") or not repo.get("default_branch"):
        raise MirrorError("目标仓库必须公开且已初始化")
    source, expected = source_release(client, folder, download=False)
    tag = source["tag_name"]
    releases = inventory(client, base)
    same = [r for r in releases if r["tag_name"] == tag]
    if len(same) > 1:
        raise MirrorError("目标仓库同一标签存在多个发行版")
    for release in releases:
        if re.fullmatch(TAG_PATTERN, release["tag_name"]) and version(release["tag_name"]) > version(tag):
            raise MirrorError("镜像已有更高版本，拒绝回退或清理")
    current = same[0] if same else None
    receipt = verification_receipt(expected, current["files"], partial=True) if current else None
    cached = (current and MARKER in (current.get("body") or "")
              and receipt and receipt in (current.get("body") or ""))
    # 按附件续传，已核验的半包也可复用；不再提前下载整版 ZIP。
    present = ({a["name"] for a in current["files"] if a["name"] in expected} if cached
               else verify_target(client, current, expected, folder) if current else set())
    verified = {a["name"]: dict(a) for a in current["files"] if a["name"] in present} if current else {}
    if (current and MARKER not in (current.get("body") or "")
            and (present != set(expected) or {a["name"] for a in current["files"]} != set(expected))):
        raise MirrorError("手动发行版必须完整匹配五个官方附件后才能接管")
    if current and not current.get("prerelease") and present != set(expected):
        raise MirrorError("已公开稳定镜像缺少附件，请先检查，不直接改写可用版本")

    incoming = sum(expected[name]["size"] for name in set(expected) - present)
    cleanup = retention_plan(releases, tag, incoming, keep, budget)
    total = sum(a["size"] for r in releases for a in r["files"])
    print(f"上游 {tag}；发行附件 {total / 1_000_000:.2f} MB；待上传 {incoming / 1_000_000:.2f} MB")
    print("待清理旧版附件: " + (", ".join(r["tag_name"] for r in cleanup) or "无"))
    # 通常在新版本验证完成后清理。空间不足时只预清理窗口外旧版，保护当前最新稳定镜像。
    stable = [r for r in releases if not r.get("prerelease") and owned_files(r)]
    latest = max(stable, key=lambda r: version(r["tag_name"]), default=None)
    before = []
    for release in cleanup:
        if total + incoming <= budget:
            break
        if latest and release["id"] == latest["id"]:
            continue
        before.append(release)
        total -= sum(a["size"] for a in owned_files(release))
    if total + incoming > budget:
        raise MirrorError("没有足够空间同时保留当前稳定版并上传新版；不会删除当前可用版")
    if before:
        print("上传前需腾出空间: " + ", ".join(r["tag_name"] for r in before))
        # 只有已完整核验的稳定镜像可以作为清理过程中的保底版本。
        if latest is None:
            raise MirrorError("找不到可保留的稳定镜像，停止预清理")
        old_tag = latest["tag_name"]
        old_files = {a["name"]: a for a in latest["files"] if a["name"] in managed_names(old_tag)}
        if len(old_files) != 5:
            raise MirrorError("当前稳定镜像不完整，停止预清理")
        manifest = old_files["SHA256SUMS.txt"]
        if not 0 < manifest["size"] <= CHECKSUM_LIMIT:
            raise MirrorError("当前稳定镜像校验清单大小异常")
        old_hash = client.download(manifest["browser_download_url"], folder / "previous-checksums.txt", manifest["size"])
        hashes = checksums((folder / "previous-checksums.txt").read_bytes(), old_tag)
        hashes["SHA256SUMS.txt"] = old_hash
        old_expected = {name: {**asset, "sha256": hashes[name]} for name, asset in old_files.items()}
        verify_target(client, latest, old_expected, folder)

    if not apply:
        print("只读预演完成：未创建、上传、更新或删除任何远端内容")
        return
    if not os.environ.get("GITEE_TOKEN"):
        raise MirrorError("应用同步需要在 GitHub Secrets 设置 GITEE_TOKEN")

    def remove(release):
        live = client.api("GET", f"{base}/releases/{release['id']}")
        live["files"] = client.pages(f"{base}/releases/{release['id']}/attach_files")
        identity = lambda files: sorted((a["id"], a["name"], a["size"]) for a in files)
        if (live.get("tag_name") != release["tag_name"] or MARKER not in (live.get("body") or "")
                or identity(live["files"]) != identity(release["files"])):
            raise MirrorError("待清理版本已被其他操作修改，停止删除")
        for asset in owned_files(release):
            client.api("DELETE", f"{base}/releases/{release['id']}/attach_files/{asset['id']}")
        remaining = client.pages(f"{base}/releases/{release['id']}/attach_files")
        if any(a["id"] in {f["id"] for f in owned_files(release)} for a in remaining):
            raise MirrorError("删除后回读仍发现旧附件，停止后续操作")
        print(f"已清理 {release['tag_name']} 的托管附件（保留标签和说明）")

    for release in before:
        remove(release)
    body = release_body(source)
    if not current:
        current = client.api("POST", base + "/releases", {
            "tag_name": tag, "name": tag, "body": body,
            "target_commitish": repo["default_branch"], "prerelease": True,
        })
    attach_url = f"{base}/releases/{current['id']}/attach_files"

    def checkpoint():
        receipt = verification_receipt(expected, list(verified.values()), partial=True)
        if verification_receipt(expected, current["files"], partial=True) != receipt:
            raise MirrorError("附件身份在核验期间变化，停止保存进度")
        if receipt:
            client.api("PATCH", f"{base}/releases/{current['id']}", {
                "tag_name": tag, "name": tag, "body": body + "\n\n" + receipt, "prerelease": True,
            })
            log(f"已保存 {tag} 的进度，核验完成 {len(present)}/{len(expected)} 个附件")

    if current.get("prerelease") and present:
        checkpoint()
    for name in sorted(set(expected) - present):
        asset = expected[name]
        if not (folder / name).exists():
            client.download(asset["browser_download_url"], folder / name, asset["size"], asset["sha256"])
        try:
            client.upload(attach_url, folder / name)
        except MirrorError:
            # 上传成功但响应丢失时，只回读校验，不重复 POST 产生同名附件。
            log(f"上传 {name} 未确认，检查服务器是否已接收")
            current["files"] = client.pages(attach_url)
            if not any(a["name"] == name for a in current["files"]):
                raise
        current["files"] = client.pages(attach_url)
        if verify_target(client, current, {name: asset}, folder) != {name}:
            raise MirrorError(f"上传后未找到附件: {name}")
        present.add(name)
        verified[name] = next(dict(a) for a in current["files"] if a["name"] == name)
        log(f"已上传并校验 {name}")
        # 最后一份也先保存，发布前的 API 波动不应丢失整版的核验进度。
        checkpoint()
    log("全部附件已核验，回读发布前状态")
    current["files"] = client.pages(attach_url)
    fresh_receipt = verification_receipt(expected, current["files"])
    if fresh_receipt != verification_receipt(expected, list(verified.values())):
        raise MirrorError("附件身份在发布前变化，保留预发布状态及旧版")
    if not fresh_receipt:
        raise MirrorError("上传后附件身份不完整，保留预发布状态及旧版")
    body += "\n\n" + fresh_receipt
    if current.get("prerelease") or current.get("body") != body or current.get("name") != tag:
        client.api("PATCH", f"{base}/releases/{current['id']}", {
            "tag_name": tag, "name": tag, "body": body, "prerelease": False,
        })
    published = client.api("GET", f"{base}/releases/{current['id']}")
    if published.get("prerelease") or published.get("tag_name") != tag or MARKER not in (published.get("body") or ""):
        raise MirrorError("发布状态回读失败，保留旧版")
    if verification_receipt(expected, client.pages(attach_url)) != fresh_receipt:
        raise MirrorError("发布后附件身份发生变化，保留旧版")
    log("镜像发布状态已确认，开始清理窗口外旧附件")
    for release in cleanup:
        if release not in before:
            remove(release)
    final_total = sum(a["size"] for r in inventory(client, base) for a in r["files"])
    if final_total > budget:
        raise MirrorError("最终容量仍超过预算，请检查并发上传的非托管附件")
    log(f"同步完成: {tag}，发行附件合计 {final_total / 1_000_000:.2f} MB")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-repo", required=True)
    parser.add_argument("--keep", type=int, default=3)
    parser.add_argument("--budget-mb", type=int, default=800)
    parser.add_argument("--apply", action="store_true", help="执行远端写入；默认只读核验和预览")
    args = parser.parse_args()
    try:
        with tempfile.TemporaryDirectory(prefix="jietuba-mirror-") as temp:
            run(Client(), args.target_repo, args.keep, args.budget_mb * 1_000_000, args.apply, Path(temp))
    except (MirrorError, ValueError, KeyError, TypeError) as exc:
        # 外部 JSON 异常仅报类型，避免将不受控响应内容写入 Actions 日志。
        print(f"同步失败: {exc if isinstance(exc, MirrorError) else type(exc).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

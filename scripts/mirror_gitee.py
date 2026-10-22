"""Mirror verified public Jietuba releases to Gitee; preview unless --apply is set."""

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

SOURCE = "1003129155/jietuba"
MARKER = f"<!-- jietuba-mirror:{SOURCE} -->"
FILE_LIMIT = 100_000_000
JSON_LIMIT = 4_000_000
CHECKSUM_LIMIT = 64_000
TAG_PATTERN = r"release-([0-9]+)\.([0-9]+)\.([0-9]+)"


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
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
                if method == "GET" and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise MirrorError(f"{host}: {method} 网络请求失败或超时") from None
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
        for attempt in range(3):
            try:
                total, hasher, started = 0, hashlib.sha256(), time.monotonic()
                with self.open("GET", url) as response, dest.open("wb") as output:
                    while block := response.read(256 * 1024):
                        total += len(block)
                        if total > size or time.monotonic() - started > 300:
                            raise MirrorError("附件超过预期大小或下载总时限")
                        output.write(block)
                        hasher.update(block)
                actual = hasher.hexdigest()
                if total != size or (digest and digest != actual):
                    raise MirrorError(f"附件大小或 SHA-256 校验失败: {dest.name}")
                return actual
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
                if attempt == 2:
                    raise MirrorError(f"附件下载失败或超时: {dest.name}") from None
                time.sleep(2 ** attempt)
        raise MirrorError("附件下载失败")

    def upload(self, url, path):
        boundary = uuid.uuid4().hex
        # multipart 写入临时文件并流式发送，避免完整安装包进入内存。
        with tempfile.TemporaryFile() as body, path.open("rb") as source:
            body.write((f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                        f"filename=\"{path.name}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode())
            shutil.copyfileobj(source, body, 256 * 1024)
            body.write(f"\r\n--{boundary}--\r\n".encode())
            length = body.tell()
            body.seek(0)
            self.api("POST", url, body, {"Content-Type": f"multipart/form-data; boundary={boundary}",
                                         "Content-Length": str(length)})


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


def verification_receipt(expected, files):
    """记录已核验的附件身份；删除重传会改变 ID，触发下一次完整校验。"""
    selected = [a for a in files if a["name"] in expected]
    if len(selected) != len(expected) or {a["name"] for a in selected} != set(expected):
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
    receipt = verification_receipt(expected, current["files"]) if current else None
    cached = (current and not current.get("prerelease") and MARKER in (current.get("body") or "")
              and receipt and receipt in (current.get("body") or ""))
    if not cached:
        # 仅新版本或附件身份变化时传输 ZIP；常规定时轮询只读取元数据和小型校验清单。
        for name, asset in expected.items():
            if name != "SHA256SUMS.txt":
                client.download(asset["browser_download_url"], folder / name, asset["size"], asset["sha256"])
    present = set(expected) if cached else (verify_target(client, current, expected, folder) if current else set())
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
    for name in sorted(set(expected) - present):
        client.upload(attach_url, folder / name)
        print(f"已上传 {name}")
    current["files"] = client.pages(attach_url)
    fresh_receipt = verification_receipt(expected, current["files"])
    if (not cached or fresh_receipt != receipt) and verify_target(client, current, expected, folder) != set(expected):
        raise MirrorError("上传后附件不完整，保留预发布状态及旧版")
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
    for release in cleanup:
        if release not in before:
            remove(release)
    final_total = sum(a["size"] for r in inventory(client, base) for a in r["files"])
    if final_total > budget:
        raise MirrorError("最终容量仍超过预算，请检查并发上传的非托管附件")
    print(f"同步完成: {tag}，发行附件合计 {final_total / 1_000_000:.2f} MB")


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

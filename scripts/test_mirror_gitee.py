import contextlib
import copy
import hashlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import mirror_gitee as mirror

TARGET = "fixture/mirror"
BASE = f"https://gitee.com/api/v5/repos/{TARGET}"
SOURCE_BASE = f"https://api.github.com/repos/{mirror.SOURCE}"


def fixture_files(tag):
    # 四种包分别使用不同内容，避免错误复用某个架构的摘要仍能通过。
    result = {
        f"jietuba_{edition}-{tag}-{arch}.zip": f"{tag}/{edition}/{arch}".encode()
        for edition in ("lite", "pp") for arch in ("x64", "arm64")
    }
    result["SHA256SUMS.txt"] = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n"
        for name, data in result.items()
    ).encode()
    return result


class FakeClient:
    """内存服务只模拟接口契约，测试不读取令牌、不访问真实网络。"""

    def __init__(self, tag="release-2.1.9"):
        self.source = {"id": 1, "tag_name": tag, "name": tag, "body": "官方说明",
                       "draft": False, "prerelease": False, "published_at": "2026-10-22"}
        self.content = {}
        self.source_assets = []
        self.releases = []
        self.events = []
        self.next_id = 100
        self.upload_count = 0
        self.fail_upload_at = None
        self.corrupt_upload_name = None
        self.omit_upload_name = None
        for name, content in fixture_files(tag).items():
            asset = self._asset(name, content, "source")
            asset.update(state="uploaded", digest="sha256:" + hashlib.sha256(content).hexdigest())
            self.source_assets.append(asset)

    def _asset(self, name, content, location):
        self.next_id += 1
        url = f"https://downloads.invalid/{location}/{self.next_id}/{name}"
        self.content[url] = content
        return {"id": self.next_id, "name": name, "size": len(content), "browser_download_url": url}

    def add_release(self, tag, managed=True, prerelease=False, extra=None):
        self.next_id += 1
        release = {"id": self.next_id, "tag_name": tag, "name": tag,
                   "body": mirror.MARKER if managed else "手动发布说明",
                   "prerelease": prerelease, "files": []}
        for name, content in {**fixture_files(tag), **(extra or {})}.items():
            release["files"].append(self._asset(name, content, "target"))
        self.releases.append(release)
        return release

    def api(self, method, url, data=None, headers=None):
        self.events.append({"kind": "api", "method": method, "url": url, "data": copy.deepcopy(data)})
        if method == "GET" and url == BASE:
            return {"public": True, "default_branch": "master"}
        if method == "GET" and url == SOURCE_BASE + "/releases/latest":
            return copy.deepcopy(self.source)
        if method == "POST" and url == BASE + "/releases":
            self.next_id += 1
            release = {**copy.deepcopy(data), "id": self.next_id, "files": []}
            self.releases.append(release)
            return copy.deepcopy(release)
        for release in self.releases:
            release_url = f"{BASE}/releases/{release['id']}"
            if method == "GET" and url == release_url:
                return copy.deepcopy(release)
            if method == "PATCH" and url == release_url:
                release.update(copy.deepcopy(data))
                return copy.deepcopy(release)
            if method == "DELETE":
                for asset in release["files"]:
                    if url == f"{release_url}/attach_files/{asset['id']}":
                        release["files"].remove(asset)
                        return None
        raise AssertionError(f"Unexpected API call: {method} {url}")

    def pages(self, url):
        self.events.append({"kind": "pages", "url": url})
        if url == SOURCE_BASE + "/releases/1/assets":
            return copy.deepcopy(self.source_assets)
        if url == BASE + "/releases":
            return copy.deepcopy(self.releases)
        for release in self.releases:
            if url == f"{BASE}/releases/{release['id']}/attach_files":
                return copy.deepcopy(release["files"])
        raise AssertionError(f"Unexpected pagination: {url}")

    def download(self, url, dest, size, digest=None):
        self.events.append({"kind": "download", "url": url, "digest": digest})
        data = self.content[url]
        actual = hashlib.sha256(data).hexdigest()
        if len(data) != size or (digest and digest != actual):
            raise mirror.MirrorError("附件大小或 SHA-256 校验失败")
        dest.write_bytes(data)
        return actual

    def upload(self, url, path):
        self.upload_count += 1
        self.events.append({"kind": "upload", "url": url, "name": path.name})
        if self.upload_count == self.fail_upload_at:
            raise mirror.MirrorError("模拟上传中断")
        if path.name == self.omit_upload_name:
            return
        data = path.read_bytes()
        if path.name == self.corrupt_upload_name:
            data = b"!" + data[1:]
        for release in self.releases:
            if url == f"{BASE}/releases/{release['id']}/attach_files":
                release["files"].append(self._asset(path.name, data, "target"))
                return
        raise AssertionError(f"Unexpected upload: {url}")

    @property
    def writes(self):
        return [event for event in self.events if event["kind"] == "upload"
                or (event["kind"] == "api" and event["method"] != "GET")]


class MirrorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.client = FakeClient()
        self.token = patch.dict(os.environ, {"GITEE_TOKEN": "synthetic-test-token"})
        self.token.start()
        self.addCleanup(self.token.stop)

    def run_mirror(self, apply=True, keep=3, budget=800_000_000):
        with contextlib.redirect_stdout(io.StringIO()):
            mirror.run(self.client, TARGET, keep, budget, apply, self.folder)

    def assert_no_writes(self):
        self.assertEqual(self.client.writes, [])

    def test_source_accepts_exactly_four_packages_and_manifest(self):
        with contextlib.redirect_stdout(io.StringIO()):
            release, assets = mirror.source_release(self.client, self.folder)
        self.assertEqual(release["tag_name"], "release-2.1.9")
        self.assertEqual(set(assets), set(fixture_files("release-2.1.9")))
        for name, data in fixture_files("release-2.1.9").items():
            self.assertEqual((self.folder / name).read_bytes(), data)
            self.assertEqual(assets[name]["sha256"], hashlib.sha256(data).hexdigest())

    def test_source_missing_each_required_asset_writes_nothing(self):
        for missing in fixture_files("release-2.1.9"):
            with self.subTest(missing=missing):
                self.client = FakeClient()
                self.client.source_assets = [a for a in self.client.source_assets if a["name"] != missing]
                with self.assertRaises(mirror.MirrorError):
                    self.run_mirror()
                self.assert_no_writes()

    def test_source_rejects_nonstable_or_unsupported_tags(self):
        for changed in ({"tag_name": "v2.1.9"}, {"tag_name": "release-2.1.9-beta"},
                        {"draft": True}, {"prerelease": True}, {"published_at": None}):
            with self.subTest(changed=changed):
                self.client = FakeClient()
                self.client.source.update(changed)
                with self.assertRaises(mirror.MirrorError):
                    self.run_mirror()
                self.assert_no_writes()

    def test_source_rejects_duplicate_pending_and_oversized_assets(self):
        for problem in ("duplicate", "pending", "oversized", "empty"):
            with self.subTest(problem=problem):
                self.client = FakeClient()
                asset = self.client.source_assets[0]
                if problem == "duplicate":
                    self.client.source_assets.append(copy.deepcopy(asset))
                elif problem == "pending":
                    asset["state"] = "new"
                else:
                    asset["size"] = mirror.FILE_LIMIT + 1 if problem == "oversized" else 0
                with self.assertRaises(mirror.MirrorError):
                    self.run_mirror()
                self.assert_no_writes()

    def test_source_rejects_api_hash_conflict_before_writes(self):
        self.client.source_assets[0]["digest"] = "sha256:" + "0" * 64
        with self.assertRaises(mirror.MirrorError):
            self.run_mirror()
        self.assert_no_writes()

    def test_source_rejects_download_hash_conflict_before_writes(self):
        asset = self.client.source_assets[0]
        self.client.content[asset["browser_download_url"]] = b"!" * asset["size"]
        with self.assertRaises(mirror.MirrorError):
            self.run_mirror()
        self.assert_no_writes()

    def test_manifest_rejects_missing_duplicate_unknown_and_invalid_hash(self):
        original = fixture_files("release-2.1.9")["SHA256SUMS.txt"].decode()
        lines = original.splitlines(keepends=True)
        variants = ["".join(lines[:-1]), original + lines[0],
                    original.replace("jietuba_lite", "../jietuba_lite", 1),
                    "z" + original[1:]]
        for manifest in variants:
            with self.subTest(manifest=manifest[:70]), self.assertRaises(mirror.MirrorError):
                mirror.checksums(manifest.encode(), "release-2.1.9")

    def test_default_cli_is_read_only(self):
        with patch("sys.argv", ["mirror_gitee.py", "--target-repo", TARGET]), \
                patch.object(mirror, "Client", return_value=self.client), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mirror.main(), 0)
        self.assert_no_writes()
        self.assertEqual(self.client.releases, [])

    def test_read_only_preview_does_not_adopt_or_cleanup_existing_releases(self):
        for version in (6, 7, 8):
            self.client.add_release(f"release-2.1.{version}")
        current = self.client.add_release("release-2.1.9", managed=False)
        self.run_mirror(apply=False)
        self.assert_no_writes()
        self.assertNotIn(mirror.MARKER, current["body"])
        self.assertEqual([len(r["files"]) for r in self.client.releases], [5, 5, 5, 5])

    def test_manual_219_is_adopted_only_after_every_file_matches(self):
        current = self.client.add_release("release-2.1.9", managed=False)
        self.run_mirror()
        writes = self.client.writes
        self.assertEqual([e["method"] for e in writes], ["PATCH"])
        self.assertIn(mirror.MARKER, current["body"])
        first_write = self.client.events.index(writes[0])
        verified = {e["url"] for e in self.client.events[:first_write]
                    if e["kind"] == "download" and e["digest"]}
        self.assertTrue({a["browser_download_url"] for a in current["files"]} <= verified)

    def test_manual_release_missing_extra_corrupt_or_duplicate_is_not_adopted(self):
        for problem in ("missing", "extra", "corrupt", "duplicate"):
            with self.subTest(problem=problem):
                self.client = FakeClient()
                current = self.client.add_release("release-2.1.9", managed=False)
                if problem == "missing":
                    current["files"].pop()
                elif problem == "extra":
                    current["files"].append(self.client._asset("readme.txt", b"user data", "target"))
                elif problem == "duplicate":
                    current["files"].append(copy.deepcopy(current["files"][0]))
                else:
                    asset = current["files"][0]
                    self.client.content[asset["browser_download_url"]] = b"!" * asset["size"]
                with self.assertRaises(mirror.MirrorError):
                    self.run_mirror()
                self.assert_no_writes()

    def test_incomplete_existing_stable_is_not_repaired_in_place(self):
        self.client.add_release("release-2.1.9")["files"].pop()
        with self.assertRaises(mirror.MirrorError):
            self.run_mirror()
        self.assert_no_writes()

    def test_new_release_is_prerelease_until_every_uploaded_file_is_verified(self):
        self.run_mirror()
        created = next(e for e in self.client.events if e.get("method") == "POST")
        published = next(e for e in self.client.events if e.get("method") == "PATCH")
        self.assertIs(created["data"]["prerelease"], True)
        self.assertIs(published["data"]["prerelease"], False)
        patch_index = self.client.events.index(published)
        verified = {e["url"] for e in self.client.events[:patch_index]
                    if e["kind"] == "download" and e["digest"]}
        current = self.client.releases[0]
        self.assertEqual(len(current["files"]), 5)
        self.assertTrue({a["browser_download_url"] for a in current["files"]} <= verified)
        self.assertFalse(current["prerelease"])

    def test_repeat_sync_is_idempotent(self):
        self.run_mirror()
        self.client.events.clear()
        self.run_mirror()
        self.assert_no_writes()
        self.assertEqual(len(self.client.releases), 1)
        downloads = [e for e in self.client.events if e["kind"] == "download"]
        self.assertEqual(len(downloads), 1)
        self.assertTrue(downloads[0]["url"].endswith("/SHA256SUMS.txt"))

    def test_changed_asset_identity_invalidates_verification_receipt(self):
        for changed in ("source_id", "source_updated", "target_id", "receipt"):
            with self.subTest(changed=changed):
                self.client = FakeClient()
                self.run_mirror()
                current = self.client.releases[0]
                if changed == "source_id":
                    self.client.source_assets[0]["id"] += 1000
                elif changed == "source_updated":
                    self.client.source_assets[0]["updated_at"] = "2026-10-22T15:30:00Z"
                elif changed == "target_id":
                    current["files"][0]["id"] += 1000
                else:
                    current["body"] = current["body"].replace("jietuba-mirror-verified:", "invalid-receipt:")
                self.client.events.clear()
                self.run_mirror()
                downloads = {e["url"] for e in self.client.events if e["kind"] == "download"}
                self.assertTrue({a["browser_download_url"] for a in self.client.source_assets} <= downloads)
                self.assertTrue({a["browser_download_url"] for a in current["files"]} <= downloads)
                self.assertFalse(any(e["kind"] == "upload" or e.get("method") == "DELETE"
                                     for e in self.client.events))

    def test_partial_prerelease_resumes_without_duplicate_uploads(self):
        current = self.client.add_release("release-2.1.9", prerelease=True)
        present = current["files"][:2]
        current["files"] = present.copy()
        self.run_mirror()
        uploads = {e["name"] for e in self.client.events if e["kind"] == "upload"}
        self.assertEqual(uploads, set(fixture_files("release-2.1.9")) - {a["name"] for a in present})
        self.assertEqual(len(current["files"]), 5)
        self.assertFalse(current["prerelease"])

    def test_upload_failure_preserves_latest_stable_and_prerelease(self):
        old = self.client.add_release("release-2.1.8")
        original_ids = {a["id"] for a in old["files"]}
        self.client.fail_upload_at = 3
        with self.assertRaises(mirror.MirrorError):
            self.run_mirror(keep=1)
        self.assertEqual({a["id"] for a in old["files"]}, original_ids)
        self.assertFalse(any(e.get("method") in ("DELETE", "PATCH") for e in self.client.events))
        self.assertTrue(next(r for r in self.client.releases if r["tag_name"] == "release-2.1.9")["prerelease"])

    def test_missing_or_corrupt_uploaded_file_blocks_publish_and_cleanup(self):
        for mode in ("omit_upload_name", "corrupt_upload_name"):
            with self.subTest(mode=mode):
                self.client = FakeClient()
                old = self.client.add_release("release-2.1.8")
                setattr(self.client, mode, "jietuba_pp-release-2.1.9-arm64.zip")
                with self.assertRaises(mirror.MirrorError):
                    self.run_mirror(keep=1)
                self.assertEqual(len(old["files"]), 5)
                self.assertFalse(any(e.get("method") in ("DELETE", "PATCH") for e in self.client.events))

    def test_sliding_window_keeps_three_versions_and_cleans_after_publish(self):
        old = [self.client.add_release(f"release-2.1.{v}") for v in (6, 7, 8)]
        self.run_mirror()
        self.assertEqual(old[0]["files"], [])
        self.assertEqual([len(r["files"]) for r in self.client.releases], [0, 5, 5, 5])
        methods = [e.get("method") for e in self.client.events]
        self.assertLess(methods.index("PATCH"), methods.index("DELETE"))

    def test_abandoned_prerelease_does_not_displace_stable_versions(self):
        self.client = FakeClient("release-2.1.4")
        stable = [self.client.add_release(f"release-2.1.{v}") for v in (1, 2)]
        partial = self.client.add_release("release-2.1.3", prerelease=True)
        partial["files"] = partial["files"][:2]
        self.run_mirror(keep=3)
        self.assertEqual([len(r["files"]) for r in stable], [5, 5])
        self.assertEqual(partial["files"], [])
        self.assertEqual(len(self.client.releases[-1]["files"]), 5)

    def test_cleanup_rechecks_ownership_and_file_identity(self):
        for changed in ("ownership", "tag", "asset_id", "asset_size", "asset_name", "extra"):
            with self.subTest(changed=changed):
                self.client = FakeClient()
                old = self.client.add_release("release-2.1.8")
                old_url = f"{BASE}/releases/{old['id']}"
                original_api = self.client.api

                def concurrent_change(method, url, data=None, headers=None, *, old_url=old_url,
                                      changed=changed, old=old, original_api=original_api):
                    if method == "GET" and url == old_url:
                        if changed == "ownership":
                            old["body"] = "由用户接管"
                        elif changed == "tag":
                            old["tag_name"] = "release-2.0.0"
                        elif changed == "extra":
                            old["files"].append(self.client._asset("notes.txt", b"new user file", "target"))
                        else:
                            key = changed.removeprefix("asset_")
                            old["files"][0][key] = "renamed.zip" if key == "name" else old["files"][0][key] + 1
                    return original_api(method, url, data, headers)

                with patch.object(self.client, "api", side_effect=concurrent_change), self.assertRaises(mirror.MirrorError):
                    self.run_mirror(keep=1)
                self.assertFalse(any(e.get("method") == "DELETE" for e in self.client.events))

    def test_failed_publication_readback_preserves_previous_stable(self):
        old = self.client.add_release("release-2.1.8")
        original_api = self.client.api

        def failed_readback(method, url, data=None, headers=None):
            response = original_api(method, url, data, headers)
            if method == "GET" and isinstance(response, dict) and response.get("tag_name") == "release-2.1.9":
                response["prerelease"] = True
            return response

        with patch.object(self.client, "api", side_effect=failed_readback), self.assertRaises(mirror.MirrorError):
            self.run_mirror(keep=1)
        self.assertEqual(len(old["files"]), 5)
        self.assertFalse(any(e.get("method") == "DELETE" for e in self.client.events))

    def test_capacity_takes_priority_over_three_version_window(self):
        old = self.client.add_release("release-2.1.7")
        latest = self.client.add_release("release-2.1.8")
        release_size = sum(a["size"] for a in latest["files"])
        self.run_mirror(budget=release_size * 2)
        self.assertEqual(old["files"], [])
        self.assertEqual(len(latest["files"]), 5)
        delete_index = next(i for i, e in enumerate(self.client.events) if e.get("method") == "DELETE")
        upload_index = next(i for i, e in enumerate(self.client.events) if e["kind"] == "upload")
        self.assertLess(delete_index, upload_index)
        verified = {e["url"] for e in self.client.events[:delete_index]
                    if e["kind"] == "download" and e["digest"]}
        self.assertTrue({a["browser_download_url"] for a in latest["files"]} <= verified)

    def test_no_capacity_for_latest_and_incoming_fails_without_deleting_latest(self):
        latest = self.client.add_release("release-2.1.8")
        release_size = sum(a["size"] for a in latest["files"])
        with self.assertRaises(mirror.MirrorError):
            self.run_mirror(keep=1, budget=release_size)
        self.assert_no_writes()
        self.assertEqual(len(latest["files"]), 5)

    def test_precleanup_refuses_corrupt_or_incomplete_latest_stable(self):
        for problem in ("corrupt", "missing"):
            with self.subTest(problem=problem):
                self.client = FakeClient()
                old = self.client.add_release("release-2.1.7")
                latest = self.client.add_release("release-2.1.8")
                release_size = sum(a["size"] for a in latest["files"])
                if problem == "missing":
                    latest["files"].pop(0)
                else:
                    asset = latest["files"][0]
                    self.client.content[asset["browser_download_url"]] = b"!" * asset["size"]
                with self.assertRaises(mirror.MirrorError):
                    self.run_mirror(budget=release_size * 2)
                self.assert_no_writes()
                self.assertEqual(len(old["files"]), 5)

    def test_precleanup_upload_failure_only_removes_oldest_not_latest(self):
        oldest = self.client.add_release("release-2.1.7")
        latest = self.client.add_release("release-2.1.8")
        release_size = sum(a["size"] for a in latest["files"])
        self.client.fail_upload_at = 2
        with self.assertRaises(mirror.MirrorError):
            self.run_mirror(budget=release_size * 2)
        self.assertEqual(oldest["files"], [])
        self.assertEqual(len(latest["files"]), 5)
        self.assertFalse(any(e.get("method") == "PATCH" for e in self.client.events))

    def test_unknown_attachments_are_not_deleted_and_count_against_budget(self):
        old = self.client.add_release("release-2.1.8", extra={"user-notes.txt": b"private fixture"})
        unknown = old["files"][-1]
        self.run_mirror(keep=1)
        self.assertEqual(old["files"], [unknown])
        new_size = sum(a["size"] for a in self.client.releases[-1]["files"])
        self.client.events.clear()
        with self.assertRaises(mirror.MirrorError):
            self.run_mirror(budget=new_size)
        self.assert_no_writes()
        self.assertEqual(old["files"], [unknown])

    def test_unmanaged_old_releases_are_not_deleted(self):
        old = self.client.add_release("release-2.1.8", managed=False)
        self.run_mirror(keep=1)
        self.assertEqual(len(old["files"]), 5)
        self.assertFalse(any(e.get("method") == "DELETE" for e in self.client.events))

    def test_retention_sorts_numeric_versions(self):
        self.client = FakeClient("release-2.1.11")
        versions = [self.client.add_release(f"release-2.1.{v}") for v in (10, 8, 9)]
        self.run_mirror(keep=3)
        self.assertEqual([len(r["files"]) for r in versions], [5, 0, 5])

    def test_newer_target_blocks_rollback_and_cleanup(self):
        self.client.add_release("release-2.1.10")
        self.client.add_release("release-2.1.8")
        with self.assertRaises(mirror.MirrorError):
            self.run_mirror(keep=1)
        self.assert_no_writes()

    def test_unknown_attachment_size_blocks_all_writes(self):
        release = self.client.add_release("release-2.1.8", extra={"notes.txt": b"fixture"})
        release["files"][-1]["size"] = None
        with self.assertRaises(mirror.MirrorError):
            self.run_mirror()
        self.assert_no_writes()


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = mirror.Client()

    def test_inventory_skips_archived_source_links_but_counts_unknown_uploads(self):
        archive = "https://gitee.com/fixture/mirror/archive/refs/tags/release-2.1.8.zip"
        releases = [
            {"id": 1, "tag_name": "release-2.1.8", "assets": [{"browser_download_url": archive}]},
            {"id": 2, "tag_name": "release-2.1.9", "assets": [{"browser_download_url": "https://gitee.com/upload"}]},
            {"id": 3, "tag_name": "release-2.2.0"},
        ]
        unknown = {"id": 20, "name": "notes.txt", "size": 100}
        with patch.object(self.client, "pages", side_effect=[releases, [unknown], []]) as pages:
            actual = mirror.inventory(self.client, BASE)
        self.assertEqual([r["files"] for r in actual], [[], [unknown], []])
        self.assertEqual([c.args[0] for c in pages.call_args_list], [
            BASE + "/releases", BASE + "/releases/2/attach_files", BASE + "/releases/3/attach_files",
        ])

    def test_pages_collects_all_pages(self):
        first = [{"id": i} for i in range(100)]
        with patch.object(self.client, "api", side_effect=[first, [{"id": 100}]]) as api:
            self.assertEqual(len(self.client.pages(BASE + "/releases")), 101)
        self.assertEqual([call.args[1] for call in api.call_args_list],
                         [BASE + "/releases?page=1&per_page=100", BASE + "/releases?page=2&per_page=100"])

    def test_pages_rejects_duplicates_and_non_list_responses(self):
        for responses in ([[{"id": i} for i in range(100)], [{"id": 0}]], [{"message": "error"}]):
            with self.subTest(responses=type(responses[0]).__name__), \
                    patch.object(self.client, "api", side_effect=responses), self.assertRaises(mirror.MirrorError):
                self.client.pages(BASE + "/releases")

    def test_get_retries_transient_errors(self):
        error = urllib.error.HTTPError(BASE, 503, "busy", {}, io.BytesIO())
        with patch.object(self.client, "open", side_effect=[error, io.BytesIO(b'{"ok": true}')]) as opened, \
                patch.object(mirror.time, "sleep") as sleep:
            self.assertEqual(self.client.api("GET", BASE), {"ok": True})
        self.assertEqual(opened.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_mutations_are_not_blindly_retried(self):
        for method in ("POST", "PATCH", "DELETE"):
            with self.subTest(method=method), \
                    patch.object(self.client, "open", side_effect=TimeoutError("secret-token")) as opened, \
                    patch.object(mirror.time, "sleep") as sleep:
                with self.assertRaises(mirror.MirrorError) as caught:
                    self.client.api(method, BASE)
                self.assertNotIn("secret-token", str(caught.exception))
                self.assertEqual(opened.call_count, 1)
                sleep.assert_not_called()

    def test_http_failure_does_not_expose_token_or_response_body(self):
        secret = "synthetic-sensitive-token"
        url = BASE + "?access_token=" + secret
        error = urllib.error.HTTPError(url, 401, secret, {}, io.BytesIO(secret.encode()))
        with patch.object(self.client, "open", side_effect=error), self.assertRaises(mirror.MirrorError) as caught:
            self.client.api("GET", url)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn("access_token", str(caught.exception))

    def test_api_tokens_are_headers_not_urls_or_json(self):
        with patch.dict(os.environ, {"GH_TOKEN": "github-fixture", "GITEE_TOKEN": "gitee-fixture"}), \
                patch.object(self.client, "open", return_value=io.BytesIO(b"{}")) as opened:
            self.client.api("PATCH", BASE, {"body": "fixture"})
        method, url, body, headers = opened.call_args.args
        self.assertEqual(method, "PATCH")
        self.assertEqual(url, BASE)
        self.assertEqual(json.loads(body), {"body": "fixture"})
        self.assertEqual(headers["Authorization"], "Bearer gitee-fixture")

    def test_api_rejects_oversized_response(self):
        with patch.object(self.client, "open", return_value=io.BytesIO(b" " * (mirror.JSON_LIMIT + 1))), \
                self.assertRaises(mirror.MirrorError):
            self.client.api("GET", BASE)

    def test_download_checks_size_and_hash(self):
        data = b"synthetic-archive"
        with tempfile.TemporaryDirectory() as temp:
            for size, digest in ((len(data) - 1, None), (len(data) + 1, None), (len(data), "0" * 64)):
                with self.subTest(size=size, digest=digest), \
                        patch.object(self.client, "open", return_value=io.BytesIO(data)), self.assertRaises(mirror.MirrorError):
                    self.client.download("https://downloads.invalid/archive.zip", Path(temp) / "archive.zip", size, digest)

    def test_download_retry_replaces_partial_file(self):
        data = b"synthetic-archive"
        class Interrupted(io.BytesIO):
            def read(self, size=-1):
                if self.tell():
                    raise OSError("connection lost")
                return super().read(3)

        with tempfile.TemporaryDirectory() as temp, \
                patch.object(self.client, "open", side_effect=[Interrupted(data), io.BytesIO(data)]), \
                patch.object(mirror.time, "sleep"):
            path = Path(temp) / "archive.zip"
            result = self.client.download("https://downloads.invalid/archive.zip", path, len(data))
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(result, hashlib.sha256(data).hexdigest())

    def test_upload_passes_stream_and_matching_content_length(self):
        observed = {}
        def capture(method, url, data, headers):
            self.assertEqual(method, "POST")
            self.assertEqual(url, BASE + "/releases/1/attach_files")
            self.assertFalse(isinstance(data, (bytes, bytearray)))
            observed["body"] = data.read()
            observed["headers"] = headers

        with tempfile.TemporaryDirectory() as temp, patch.object(self.client, "api", side_effect=capture):
            path = Path(temp) / "archive.zip"
            path.write_bytes(b"fixture archive content")
            self.client.upload(BASE + "/releases/1/attach_files", path)
        self.assertEqual(int(observed["headers"]["Content-Length"]), len(observed["body"]))
        self.assertIn(b'filename="archive.zip"', observed["body"])
        self.assertIn(b"fixture archive content", observed["body"])
        self.assertIn("multipart/form-data; boundary=", observed["headers"]["Content-Type"])

    def test_credential_redirects_and_https_downgrades_are_blocked(self):
        redirect = mirror.SafeRedirect()
        for headers, destination in (({"Authorization": "Bearer fixture"}, "https://other.invalid/file"),
                                     ({}, "http://downloads.invalid/file")):
            with self.subTest(destination=destination):
                request = urllib.request.Request(BASE, headers=headers)
                with self.assertRaises(mirror.MirrorError):
                    redirect.redirect_request(request, None, 302, "Found", {}, destination)


if __name__ == "__main__":
    unittest.main()

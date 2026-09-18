import copy
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock
import urllib.parse
import uuid

from updater import discovery as d


class Response(io.BytesIO):
    def __init__(self, data=b"", code=200, headers=None):
        super().__init__(data)
        self.code = code
        self.headers = headers or {}


def release(version="1.1.0", identifier=1, prerelease=False, platform="pi5"):
    name = f"cloudplay-os-{version}-{platform}.tar.zst"
    tag = "v" + version
    assets = []
    for index, suffix in enumerate(("", ".minisig", ".catalog.json", ".catalog.json.minisig")):
        asset_name = name + suffix
        url = ("https://github.com/sslivins/cloudplay-os/releases/download/"
               + urllib.parse.quote(tag, safe="") + "/" + urllib.parse.quote(asset_name, safe=""))
        assets.append(dict(name=asset_name, id=identifier * 10 + index, size=4,
                           browser_download_url=url, state="uploaded"))
    return dict(id=identifier, tag_name=tag, draft=False, prerelease=prerelease,
                assets=assets, body="Release notes", published_at="2026-09-17T00:00:00Z")


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="cloudplay-ota-discovery-"))
        self.now = 100000
        self.client = d.Discovery(clock=lambda: self.now)

    def tearDown(self):
        shutil.rmtree(self.work)

    def api_pages(self, pages):
        self.client._opener.open = mock.Mock(side_effect=[
            Response(json.dumps(page).encode(), headers={"ETag": f'"page-{i}"'})
            for i, page in enumerate(pages)])

    def test_semver_not_release_date_selects_highest(self):
        self.api_pages([[release("1.10.0", 1), release("1.9.0", 2), release("2.0.0-beta.2", 3, True)]])
        selected = self.client.check("1.0.0")
        self.assertEqual(selected["version"], "2.0.0-beta.2")
        for field in ("release_id", "notes", "published_at", "bundle_url", "signature_url", "size", "name"):
            self.assertIn(field, selected)
        self.assertEqual(self.client.last_successful_check, self.now)

    def test_stable_excludes_prerelease_flag_and_semver(self):
        self.client.channel = "stable"
        self.api_pages([[release("2.0.0", 1, True), release("3.0.0-beta.1", 2, False), release()]])
        self.assertEqual(self.client.check("1.0.0")["version"], "1.1.0")

    def test_switch_beta_to_stable_never_downgrades(self):
        self.client.channel = "stable"
        self.api_pages([[release("1.0.0")]])
        self.assertIsNone(self.client.check("1.1.0-beta.1"))

    def test_paginated_highest_version(self):
        self.api_pages([[release("1.1.0", i + 1) for i in range(100)], [release("9.0.0", 101)]])
        self.assertEqual(self.client.check("1.0.0")["version"], "9.0.0")
        calls = self.client._opener.open.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIn("page=2", calls[1].args[0].full_url)

    def test_pagination_limit_is_error_not_partial_result(self):
        self.api_pages([[release("1.1.0", i + 1) for i in range(100)]])
        with mock.patch.object(d, "MAX_PAGES", 1), self.assertRaisesRegex(d.DiscoveryError, "LIMIT"):
            self.client.check("1.0.0")
        self.assertIsNone(self.client.last_successful_check)

    def test_etag_cache_and_schedule(self):
        self.api_pages([[release()]])
        first = self.client.check("1.0.0")
        self.assertEqual(self.client.check("1.0.0"), first)
        self.assertEqual(self.client.check("1.0.0", force=True), first)
        self.assertEqual(self.client._opener.open.call_count, 1)
        self.now += 21600
        self.client._opener.open = mock.Mock(return_value=Response(code=304))
        self.assertEqual(self.client.check("1.0.0"), first)
        request = self.client._opener.open.call_args.args[0]
        self.assertEqual(request.get_header("If-none-match"), '"page-0"')

    def test_persistent_cache(self):
        cache = self.work / "discovery.json"
        self.client.cache_file = cache
        self.api_pages([[release()]])
        first = self.client.check("1.0.0")
        second = d.Discovery(cache_file=cache, clock=lambda: self.now)
        with mock.patch.object(second, "_request") as request:
            self.assertEqual(second.check("1.0.0"), first)
        request.assert_not_called()

    def test_bad_cache_is_ignored(self):
        cache = self.work / "discovery.json"
        cache.write_text("not json")
        client = d.Discovery(cache_file=cache, clock=lambda: self.now)
        self.assertEqual(client._pages, {})

    def test_failure_backoff_and_stale_time(self):
        self.client._opener.open = mock.Mock(side_effect=OSError("offline"))
        with self.assertRaisesRegex(d.DiscoveryError, "NETWORK"):
            self.client.check("1.0.0")
        self.assertEqual(self.client.next_check_at, self.now + 60)
        with self.assertRaisesRegex(d.DiscoveryError, "BACKOFF"):
            self.client.check("1.0.0", force=True)
        self.assertEqual(self.client._opener.open.call_count, 1)
        self.assertIsNone(self.client.last_successful_check)
        self.now += 60
        with self.assertRaises(d.DiscoveryError):
            self.client.check("1.0.0")
        self.assertEqual(self.client.next_check_at, self.now + 120)

    def test_failed_refresh_keeps_complete_old_pages(self):
        self.api_pages([[release()]])
        old = self.client.check("1.0.0")
        self.now += 21600
        self.client._opener.open = mock.Mock(side_effect=[
            Response(json.dumps([release("2.0.0", i + 1) for i in range(100)]).encode()),
            OSError("offline")])
        with self.assertRaises(d.DiscoveryError):
            self.client.check("1.0.0")
        self.assertEqual(self.client.check("1.0.0"), old)

    def test_untrusted_malformed_releases_skipped(self):
        variants = []
        for mutate in (
                lambda r: r.update(draft=True),
                lambda r: r.update(id=True),
                lambda r: r.update(tag_name="branch-main"),
                lambda r: r["assets"].pop(),
                lambda r: r["assets"][0].update(size=d.MAX_COMPRESSED + 1),
                lambda r: r["assets"][0].update(size=-1),
                lambda r: r["assets"][0].update(browser_download_url="https://evil.test/file"),
                lambda r: r["assets"][0].update(name="../file"),
                lambda r: r["assets"].append(copy.deepcopy(r["assets"][0]))):
            candidate = release()
            mutate(candidate)
            variants.append(candidate)
        self.api_pages([variants])
        self.assertIsNone(self.client.check("1.0.0"))

    def test_platform_selection_and_ambiguity(self):
        candidate = release()
        candidate["assets"].extend(release(platform="other")["assets"])
        self.assertIsNone(self.client._release(candidate))
        self.client.platform = "pi5"
        self.assertIsNotNone(self.client._release(candidate))

    def test_bad_redirect_blocked_before_second_request(self):
        asset = release()["assets"][0]["browser_download_url"]
        for target in ("http://github.com/sslivins/cloudplay-os/releases/download/v1/a",
                       "https://evil.test/payload", "https://github.com/other/repo/releases/download/v1/a",
                       "https://github.com/sslivins/cloudplay-os/releases/download/../evil",
                       "https://github.com/sslivins/cloudplay-os/releases/download/%2e%2e/evil",
                       "https://user:pass@github.com/sslivins/cloudplay-os/releases/download/v1/a",
                       "https://github.com:444/sslivins/cloudplay-os/releases/download/v1/a",
                       "https://release-assets.githubusercontent.com.evil.test/a"):
            self.client._opener.open = mock.Mock(return_value=Response(code=302, headers={"Location": target}))
            with self.subTest(target=target), self.assertRaises(d.DiscoveryError):
                self.client._request(asset)
            self.assertEqual(self.client._opener.open.call_count, 1)

    def test_each_redirect_revalidated(self):
        url = release()["assets"][0]["browser_download_url"]
        self.client._opener.open = mock.Mock(side_effect=[
            Response(code=302, headers={"Location": "https://release-assets.githubusercontent.com/a"}),
            Response(code=307, headers={"Location": "http://evil.test/b"})])
        with self.assertRaisesRegex(d.DiscoveryError, "URL"):
            self.client._request(url)
        self.assertEqual(self.client._opener.open.call_count, 2)

    def test_allowed_redirect(self):
        url = release()["assets"][0]["browser_download_url"]
        self.client._opener.open = mock.Mock(side_effect=[
            Response(code=302, headers={"Location": "https://release-assets.githubusercontent.com/a?signature=opaque"}),
            Response(b"data")])
        with self.client._request(url) as response:
            self.assertEqual(response.read(), b"data")

    def test_redirect_loop_limit(self):
        url = release()["assets"][0]["browser_download_url"]
        self.client._opener.open = mock.Mock(side_effect=lambda *args, **kwargs:
                                            Response(code=302, headers={"Location": url}))
        with self.assertRaisesRegex(d.DiscoveryError, "REDIRECT"):
            self.client._request(url)
        self.assertEqual(self.client._opener.open.call_count, d.MAX_REDIRECTS + 1)

    def test_api_redirect_cannot_leave_repository(self):
        url = "https://api.github.com/repos/sslivins/cloudplay-os/releases?per_page=100&page=1"
        self.client._opener.open = mock.Mock(return_value=Response(
            code=302, headers={"Location": "https://api.github.com/repos/other/repo/releases"}))
        with self.assertRaisesRegex(d.DiscoveryError, "URL"):
            self.client._request(url, api=True)
        self.assertEqual(self.client._opener.open.call_count, 1)

    def test_api_response_byte_cap_and_encoding(self):
        with mock.patch.object(d, "API_LIMIT", 10):
            self.client._opener.open = mock.Mock(return_value=Response(b"x" * 11))
            with self.assertRaisesRegex(d.DiscoveryError, "LIMIT"):
                self.client.check("1.0.0")
        self.client._opener.open = mock.Mock(return_value=Response(headers={"Content-Encoding": "gzip"}))
        with self.assertRaisesRegex(d.DiscoveryError, "HTTP"):
            self.client._request(release()["assets"][0]["browser_download_url"])

    def download_setup(self, data=b"data"):
        remote = release()
        selected = self.client._release(remote)
        self.client._api = mock.Mock(return_value=remote)
        self.client._request = mock.Mock(side_effect=lambda *args, **kwargs: Response(data))
        return selected

    def test_download_all_four_artifacts(self):
        selected = self.download_setup()
        progress = mock.Mock()
        bundle, signature = self.client.download(selected, self.work / "download", progress=progress)
        self.assertEqual(bundle.read_bytes(), b"data")
        self.assertEqual(signature.read_bytes(), b"data")
        self.assertTrue(bundle.with_name(bundle.name + ".catalog.json").exists())
        self.assertTrue(bundle.with_name(bundle.name + ".catalog.json.minisig").exists())
        self.assertEqual(progress.call_args.args, (16, 16))
        self.assertEqual(len(list(bundle.parent.iterdir())), 4)

    def test_download_rejects_changed_ids_and_arbitrary_urls(self):
        for field, value in (("bundle_url", "https://evil.test/x"), ("release_id", -1),
                             ("name", "../../x")):
            selected = self.download_setup()
            selected[field] = value
            with self.subTest(field=field), self.assertRaises(d.DiscoveryError):
                self.client.download(selected, self.work / "download")
            self.client._request.assert_not_called()
        selected = self.download_setup()
        selected["assets"][""]["id"] += 1
        with self.assertRaisesRegex(d.DiscoveryError, "RELEASE_CHANGED"):
            self.client.download(selected, self.work / "download")

    def test_download_size_caps_ignore_content_length(self):
        for data in (b"shorter" * 100, b"x"):
            selected = self.download_setup(data=data)
            directory = self.work / ("download-" + str(len(data)))
            with self.subTest(size=len(data)), self.assertRaises(d.DiscoveryError):
                self.client.download(selected, directory)
            self.assertEqual(list(directory.iterdir()), [])

    def test_cancel_cleans_partial_and_completed_files(self):
        selected = self.download_setup()
        progress_count = []
        def progress(received, total):
            progress_count.append(received)
        with self.assertRaisesRegex(d.DiscoveryError, "CANCELLED"):
            self.client.download(selected, self.work / "download", progress=progress,
                                 cancel=lambda: len(progress_count) >= 2)
        self.assertEqual(list((self.work / "download").iterdir()), [])

    def test_does_not_overwrite_existing_or_resume_partial(self):
        selected = self.download_setup()
        directory = self.work / "download"
        directory.mkdir(mode=0o700)
        existing = directory / selected["name"]
        existing.write_bytes(b"keep")
        with self.assertRaisesRegex(d.DiscoveryError, "IMMUTABLE"):
            self.client.download(selected, directory)
        self.assertEqual(existing.read_bytes(), b"keep")
        existing.unlink()
        partial = directory / (selected["name"] + ".part")
        partial.write_bytes(b"old")
        with self.assertRaisesRegex(d.DiscoveryError, "DOWNLOAD"):
            self.client.download(selected, directory)
        self.assertEqual(partial.read_bytes(), b"old")

    def test_304_without_cache_rejected(self):
        self.client._opener.open = mock.Mock(return_value=Response(code=304))
        with self.assertRaisesRegex(d.DiscoveryError, "CACHE"):
            self.client.check("1.0.0")


if __name__ == "__main__":
    unittest.main()

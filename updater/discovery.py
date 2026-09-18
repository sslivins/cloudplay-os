"""Bounded GitHub discovery; release metadata is never an installation trust root."""
from __future__ import annotations

import copy
import http.client
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .artifacts import (ArtifactError, SemVer, MAX_COMPRESSED, MAX_SIGNATURE,
                        MAX_CATALOG, canonical_json)

API_LIMIT = 2 * 1024**2
CACHE_LIMIT = 8 * 1024**2
MAX_PAGES = 20
MAX_REDIRECTS = 5
MIN_MANUAL_CHECK = 1800
ASSET_HOSTS = frozenset({"github.com", "release-assets.githubusercontent.com",
                        "objects.githubusercontent.com"})


class DiscoveryError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_chunk(response, size):
    try:
        return getattr(response, "read1", response.read)(size)
    except (OSError, http.client.HTTPException) as exc:
        raise DiscoveryError("NETWORK", "response stream failed") from exc


class Discovery:
    def __init__(self, repo="sslivins/cloudplay-os", channel="beta", *,
                 platform=None, cache_file: Path | None = None, clock=time.time):
        if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repo):
            raise DiscoveryError("CONFIG", "invalid repository")
        if channel not in ("stable", "beta"):
            raise DiscoveryError("CONFIG", "invalid channel")
        if platform is not None and not re.fullmatch(r"[A-Za-z0-9.-]{1,64}", platform):
            raise DiscoveryError("CONFIG", "invalid platform")
        self.repo, self.channel, self.platform = repo, channel, platform
        self.cache_file, self.clock = Path(cache_file) if cache_file else None, clock
        self.last_successful_check = None
        self.next_check_at = 0
        self._failures = 0
        self._pages = {}
        self._complete = False
        self._opener = urllib.request.build_opener(_NoRedirect())
        self._load_cache()

    def _validate_url(self, url: str, *, api=False):
        if not isinstance(url, str) or len(url) > 8192 or any(ord(c) <= 32 for c in url):
            raise DiscoveryError("URL", "invalid URL")
        try:
            parsed = urllib.parse.urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise DiscoveryError("URL", "invalid URL authority") from exc
        if (parsed.scheme != "https" or parsed.username is not None
                or parsed.password is not None or port not in (None, 443)
                or parsed.fragment or "\\" in url):
            raise DiscoveryError("URL", "only credential-free HTTPS URLs are permitted")
        if api:
            if (parsed.hostname != "api.github.com"
                    or not re.fullmatch(re.escape(f"/repos/{self.repo}/releases") + r"(?:/[1-9][0-9]*)?", parsed.path)):
                raise DiscoveryError("URL", "API URL is outside configured release repository")
        else:
            if parsed.hostname not in ASSET_HOSTS:
                raise DiscoveryError("URL", "release asset host is not allowlisted")
            if parsed.hostname == "github.com":
                decoded = urllib.parse.unquote(parsed.path)
                prefix = f"/{self.repo}/releases/download/"
                suffix = decoded[len(prefix):].split("/") if decoded.startswith(prefix) else []
                if (len(suffix) != 2 or any(p in ("", ".", "..") for p in suffix)
                        or "%" in decoded or "\\" in decoded):
                    raise DiscoveryError("URL", "release asset is outside configured repository")
        return parsed

    def _request(self, url, *, api=False, etag=None):
        headers = {"User-Agent": "cloudplay-os-updater/1", "Accept-Encoding": "identity",
                   "Accept": "application/vnd.github+json" if api else "application/octet-stream"}
        if etag:
            if not isinstance(etag, str) or len(etag) > 512 or "\r" in etag or "\n" in etag:
                raise DiscoveryError("CACHE", "invalid cached ETag")
            headers["If-None-Match"] = etag
        for hop in range(MAX_REDIRECTS + 1):
            self._validate_url(url, api=api)
            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                response = self._opener.open(request, timeout=30)
            except urllib.error.HTTPError as exc:
                response = exc
            except (OSError, urllib.error.URLError) as exc:
                raise DiscoveryError("NETWORK", "release server is unavailable") from exc
            status = response.code
            if status in (301, 302, 303, 307, 308):
                target = response.headers.get("Location")
                response.close()
                if hop == MAX_REDIRECTS or not target:
                    raise DiscoveryError("REDIRECT", "too many or invalid redirects")
                url = urllib.parse.urljoin(url, target)
                # Validation occurs before the next request, never after following it.
                continue
            if status not in (200, 304) or status == 304 and not api:
                response.close()
                raise DiscoveryError("HTTP", f"release server returned HTTP {status}")
            if response.headers.get("Content-Encoding", "identity") not in ("", "identity"):
                response.close()
                raise DiscoveryError("HTTP", "encoded response bodies are forbidden")
            return response
        raise DiscoveryError("REDIRECT", "redirect limit")

    @staticmethod
    def _bounded_read(response, limit):
        parts, count = [], 0
        deadline = time.monotonic() + 60
        while True:
            if time.monotonic() > deadline:
                raise DiscoveryError("TIMEOUT", "response time limit exceeded")
            chunk = _read_chunk(response, min(65536, limit + 1 - count))
            if not chunk:
                return b"".join(parts)
            count += len(chunk)
            if count > limit:
                raise DiscoveryError("LIMIT", "server response exceeds byte limit")
            parts.append(chunk)

    def _api(self, url, *, use_cache=True):
        cached = self._pages.get(url) if use_cache else None
        with self._request(url, api=True, etag=cached.get("etag") if cached else None) as response:
            if response.code == 304:
                if not cached:
                    raise DiscoveryError("CACHE", "304 response without cached page")
                return copy.deepcopy(cached["data"])
            body = self._bounded_read(response, API_LIMIT)
            try:
                data = json.loads(body)
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise DiscoveryError("API", "invalid release JSON") from exc
            etag = response.headers.get("ETag")
            if etag and (len(etag) > 512 or "\r" in etag or "\n" in etag):
                raise DiscoveryError("API", "invalid server ETag")
            if use_cache:
                self._pages[url] = {"etag": etag, "data": data}
            return data

    def _load_cache(self):
        if not self.cache_file:
            return
        try:
            if self.cache_file.is_symlink() or self.cache_file.stat().st_size > CACHE_LIMIT:
                return
            value = json.loads(self.cache_file.read_bytes())
            if (value["repo"], value["channel"], value["platform"]) != (self.repo, self.channel, self.platform):
                return
            pages = value["pages"]
            if not isinstance(pages, dict) or len(pages) > MAX_PAGES:
                return
            for url, page in pages.items():
                self._validate_url(url, api=True)
                if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                    return
            last = value["last_successful_check"]
            next_check = value["next_check_at"]
            failures = value["failures"]
            if (last is not None and (type(last) not in (int, float) or not 0 <= last <= self.clock())
                    or type(next_check) not in (int, float)
                    or not 0 <= next_check <= self.clock() + 21600
                    or type(failures) is not int or not 0 <= failures <= 20
                    or type(value["complete"]) is not bool):
                return
            self._pages, self._complete = pages, value["complete"]
            self.last_successful_check, self.next_check_at = last, next_check
            self._failures = failures
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            return

    def _save_cache(self):
        if not self.cache_file:
            return
        value = dict(repo=self.repo, channel=self.channel, platform=self.platform,
                     pages=self._pages, complete=self._complete,
                     last_successful_check=self.last_successful_check,
                     next_check_at=self.next_check_at, failures=self._failures)
        data = canonical_json(value)
        if len(data) > CACHE_LIMIT:
            # Never persist an incomplete page set as a complete check.
            value.update(pages={}, complete=False)
            data = canonical_json(value)
        self.cache_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = self.cache_file.with_name(self.cache_file.name + "." + uuid.uuid4().hex + ".new")
        created = False
        try:
            with staging.open("xb") as output:
                created = True
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(staging, self.cache_file)
            if os.name == "posix":
                descriptor = os.open(self.cache_file.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            if created:
                staging.unlink(missing_ok=True)

    def _release(self, release):
        if not isinstance(release, dict) or release.get("draft") is not False:
            return None
        tag = release.get("tag_name", "")
        if not isinstance(tag, str):
            return None
        version = tag[1:] if tag.startswith("v") else tag
        try:
            parsed = SemVer.parse(version)
        except ArtifactError:
            return None
        if self.channel == "stable" and (release.get("prerelease") is not False or parsed.prerelease):
            return None
        rid = release.get("id")
        if type(rid) is not int or rid <= 0:
            return None
        assets = release.get("assets")
        if not isinstance(assets, list) or len(assets) > 100:
            return None
        indexed = {}
        for asset in assets:
            if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
                return None
            name = asset["name"]
            if name in indexed:
                return None
            indexed[name] = asset
        platform_pattern = re.escape(self.platform) if self.platform else r"[A-Za-z0-9.-]{1,64}"
        pattern = re.compile(r"cloudplay-os-" + re.escape(version) + "-" + platform_pattern + r"\.tar\.zst")
        bundles = [name for name in indexed if len(name) <= 255 and pattern.fullmatch(name)]
        if len(bundles) != 1:
            return None
        name = bundles[0]
        selected = {}
        for suffix, maximum in (("", MAX_COMPRESSED), (".minisig", MAX_SIGNATURE),
                                (".catalog.json", MAX_CATALOG), (".catalog.json.minisig", MAX_SIGNATURE)):
            asset = indexed.get(name + suffix)
            if not asset:
                return None
            url = (f"https://github.com/{self.repo}/releases/download/"
                   f"{urllib.parse.quote(tag, safe='')}/{urllib.parse.quote(name + suffix, safe='')}")
            if (asset.get("browser_download_url") != url
                    or type(asset.get("id")) is not int or asset["id"] <= 0
                    or type(asset.get("size")) is not int or not 0 < asset["size"] <= maximum
                    or asset.get("state", "uploaded") != "uploaded"):
                return None
            selected[suffix] = {"url": url, "id": asset["id"], "size": asset["size"]}
        notes, published = release.get("body") or "", release.get("published_at")
        if not isinstance(notes, str) or not isinstance(published, str) or len(published) > 64:
            return None
        return dict(version=version, release_id=rid, notes=notes[:16384], published_at=published,
                    bundle_url=selected[""]["url"], signature_url=selected[".minisig"]["url"],
                    catalog_url=selected[".catalog.json"]["url"],
                    catalog_signature_url=selected[".catalog.json.minisig"]["url"],
                    size=selected[""]["size"], name=name, tag=tag, assets=selected)

    def _choose(self, releases, current_version):
        current = SemVer.parse(current_version)
        choices = [result for release in releases if (result := self._release(release))
                   and SemVer.parse(result["version"]) > current]
        if not choices:
            return None
        choices.sort(key=lambda item: (SemVer.parse(item["version"]), item["release_id"]), reverse=True)
        # Build metadata does not establish precedence; deterministic release-ID tie break.
        return copy.deepcopy(choices[0])

    def check(self, current_version: str, *, force=False) -> dict | None:
        SemVer.parse(current_version)
        now = self.clock()
        too_soon = self.last_successful_check is not None and now < self.last_successful_check + MIN_MANUAL_CHECK
        if now < self.next_check_at and (not force or self._failures or too_soon):
            if not self._complete:
                raise DiscoveryError("BACKOFF", "next discovery attempt is scheduled later")
            return self._choose([r for page in self._pages.values() for r in page["data"]], current_version)
        active = {}
        releases = []
        old_pages = copy.deepcopy(self._pages)
        try:
            for page in range(1, MAX_PAGES + 1):
                url = f"https://api.github.com/repos/{self.repo}/releases?per_page=100&page={page}"
                data = self._api(url)
                if not isinstance(data, list) or len(data) > 100:
                    raise DiscoveryError("API", "invalid release page")
                active[url] = self._pages[url]
                releases.extend(data)
                if len(data) < 100:
                    break
            else:
                raise DiscoveryError("LIMIT", "release pagination exceeds bounded scan")
            selected = self._choose(releases, current_version)
            self._pages, self._complete = active, True
            self.last_successful_check = now
            self.next_check_at, self._failures = now + 21600, 0
            self._save_cache()
            return selected
        except (DiscoveryError, OSError) as exc:
            self._pages = old_pages
            self._failures = min(self._failures + 1, 20)
            self.next_check_at = now + min(21600, 60 * 2 ** (self._failures - 1))
            try:
                self._save_cache()
            except OSError:
                pass
            if isinstance(exc, DiscoveryError):
                raise
            raise DiscoveryError("CACHE", "could not persist discovery state") from exc

    def download(self, release: dict, directory: Path, progress=None, cancel=None) -> tuple[Path, Path]:
        """Re-fetch immutable IDs, download four assets; never install or trust them.

        ``progress(received_bytes, total_bytes)`` spans all four assets.
        ``cancel()`` returning true aborts and removes this call's partial files.
        Resume is intentionally unsupported: each new attempt starts at byte zero.
        """
        if not isinstance(release, dict) or type(release.get("release_id")) is not int or release["release_id"] <= 0:
            raise DiscoveryError("RELEASE", "invalid selected release")
        fresh = self._release(self._api(
            f"https://api.github.com/repos/{self.repo}/releases/{release['release_id']}", use_cache=False))
        if not fresh or any(fresh[key] != release.get(key) for key in
                            ("release_id", "version", "tag", "name", "size", "assets",
                             "bundle_url", "signature_url", "catalog_url", "catalog_signature_url")):
            raise DiscoveryError("RELEASE_CHANGED", "release assets changed since discovery")
        directory = Path(directory).absolute()
        for parent in (directory, *directory.parents):
            if parent.is_symlink():
                raise DiscoveryError("DIRECTORY", "download path contains a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        if not directory.is_dir():
            raise DiscoveryError("DIRECTORY", "download path is not a directory")
        if os.name == "posix":
            info = directory.stat()
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise DiscoveryError("DIRECTORY", "download directory must be private and caller-owned")
        received = 0
        deadline = time.monotonic() + 1800
        total = sum(a["size"] for a in fresh["assets"].values())
        created, completed = [], []
        try:
            for suffix, asset in fresh["assets"].items():
                destination = directory / (fresh["name"] + suffix)
                partial = destination.with_name(destination.name + ".part")
                if destination.exists() or destination.is_symlink():
                    raise DiscoveryError("IMMUTABLE", "download destination already exists")
                with partial.open("xb") as output:
                    created.append(partial)
                    if cancel and cancel():
                        raise DiscoveryError("CANCELLED", "download cancelled")
                    with self._request(asset["url"]) as response:
                        size = 0
                        while True:
                            if time.monotonic() > deadline:
                                raise DiscoveryError("TIMEOUT", "download time limit exceeded")
                            if cancel and cancel():
                                raise DiscoveryError("CANCELLED", "download cancelled")
                            chunk = _read_chunk(response, min(1024 * 1024, asset["size"] + 1 - size))
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > asset["size"]:
                                raise DiscoveryError("LIMIT", "asset exceeds advertised bounded size")
                            output.write(chunk)
                            received += len(chunk)
                            if progress:
                                progress(received, total)
                        if size != asset["size"]:
                            raise DiscoveryError("TRUNCATED", "incomplete asset download")
                    output.flush()
                    os.fsync(output.fileno())
                # link() is an atomic no-overwrite publication, unlike replace().
                os.link(partial, destination)
                completed.append(destination)
                partial.unlink()
            if os.name == "posix":
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            return directory / fresh["name"], directory / (fresh["name"] + ".minisig")
        except (OSError, urllib.error.URLError) as exc:
            raise DiscoveryError("DOWNLOAD", "download IO failed") from exc
        finally:
            for partial in created:
                partial.unlink(missing_ok=True)
            if len(completed) != 4:
                for path in completed:
                    path.unlink(missing_ok=True)

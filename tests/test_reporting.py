import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "launcher"))
import reporting


class ReportTests(unittest.TestCase):
    def test_only_preview_fields_are_encoded_and_no_raw_error_or_identifiers(self):
        status = {
            "phase": "failed", "error": {"code": "SPACE", "message": "secret-user@example.com"},
            "ssid": "private-wifi", "serial": "12345678", "ip": "192.168.1.114",
            "current_version": "untrusted-profile-data", "notes": "secret-notes",
        }
        summary, url = reporting.build_report("0.1.0-beta.23", status, 8 * 1024**3)
        parts = urlsplit(url)
        self.assertEqual(parts.scheme, "https")
        self.assertEqual(parts.netloc, "github.com")
        self.assertEqual(parts.path, "/sslivins/cloudplay-os/issues/new")
        fields = parse_qs(parts.query)
        self.assertEqual(set(fields), {"title", "body"})
        self.assertEqual(fields["body"], ["What happened?\n\n\n" + summary])
        self.assertEqual(summary, "OS: 0.1.0-beta.23\nUpdate: failed\nError: SPACE\nFree: ~8 GiB")
        for forbidden in ("secret", "private", "192.168", "12345678", "untrusted"):
            self.assertNotIn(forbidden, url)

    def test_all_supported_values_fit_payload_and_fixed_qr_capacity(self):
        longest_phase = max(reporting.PHASES, key=len)
        longest_code = max(reporting.ERROR_CODES, key=len)
        _, url = reporting.build_report("9999.9999.9999-beta.9999",
                                       {"phase": longest_phase, "error": {"code": longest_code}},
                                       99999 * 1024**3)
        self.assertLessEqual(len(url.encode("ascii")), reporting.MAX_URL_BYTES)
        width, pixels = reporting.qr_pixels(url, 3)
        self.assertEqual(width, 69 * 3)
        self.assertEqual(len(pixels), width * width * 3)
        self.assertEqual(pixels[:width * 12 * 3], b"\xff" * (width * 12 * 3))
        self.assertIn(b"\x00", pixels)

    def test_unknown_status_text_never_leaks(self):
        summary, url = reporting.build_report("1.2.3", {
            "phase": "MY_PRIVATE_NETWORK", "error": {"code": "PRIVATE_EMAIL", "message": "private"}},
            512 * 1024**2)
        self.assertIn("Update: unknown\nError: OTHER\nFree: ~1 GiB", summary)
        self.assertNotIn("PRIVATE", url)

    def test_malformed_inputs_and_unbounded_payload_are_rejected(self):
        for version in (None, [], "1.2.3+private-host", "1.2.3\nsecret", "9" * 400):
            with self.subTest(version=version), self.assertRaises(ValueError):
                reporting.build_report(version, {}, 0)
        for space in (-1, True, "100", 100000 * 1024**3):
            with self.subTest(space=space), self.assertRaises(ValueError):
                reporting.build_report("1.2.3", {}, space)
        with self.assertRaises(ValueError):
            reporting.qr_pixels(reporting.ISSUES + "?body=" + "a" * 400, 5)
        with self.assertRaises(ValueError):
            reporting.qr_pixels("https://example.com/", 5)

    def test_collection_uses_installed_release_not_pending_state(self):
        with tempfile.TemporaryDirectory() as temp:
            release = Path(temp) / "release.json"
            release.write_text(json.dumps({"version": "0.1.0-beta.23", "source_commit": "not sent"}))
            with patch.object(reporting.shutil, "disk_usage", return_value=SimpleNamespace(free=0)):
                summary, _ = reporting.collect_report(
                    {"current_version": "0.1.0-beta.22", "phase": "promoted"},
                    release_file=release, data_path=temp)
                self.assertIn("OS: 0.1.0-beta.23", summary)
                self.assertIn("Error: none", summary)
                release.write_text("x" * 4097)
                with self.assertRaises(ValueError):
                    reporting.collect_report({}, release_file=release, data_path=temp)

    def test_missing_release_is_explicit_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(FileNotFoundError):
                reporting.collect_report({}, release_file=Path(temp) / "missing.json")


if __name__ == "__main__":
    unittest.main()

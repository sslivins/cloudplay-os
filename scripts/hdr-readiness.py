#!/usr/bin/env python3
"""Read-only version prerequisite check; never enables or certifies HDR."""
import json
import re
import subprocess


def assess(version_output):
    def version(pattern):
        match = re.search(pattern, version_output)
        return tuple(map(int, match.groups())) if match else None

    labwc = version(r"\blabwc\s+v?(\d+)\.(\d+)\.(\d+)")
    wlroots = version(r"\bwlroots-(\d+)\.(\d+)\.(\d+)")
    if labwc is not None and labwc < (0, 20, 0):
        assessment = "blocked-by-labwc-version"
    elif wlroots is not None and wlroots < (0, 20, 1):
        assessment = "blocked-by-wlroots-version"
    elif labwc is None or wlroots is None:
        assessment = "unknown-version-prerequisites"
    elif re.search(r"\d+\.\d+\.\d+[-~](?:rc|alpha|beta)", version_output, re.IGNORECASE):
        assessment = "unknown-prerelease-version"
    else:
        assessment = "version-floors-met-hdr-still-unvalidated"
    return {
        "assessment": assessment,
        "installed_binary_version_output": version_output.strip(),
        "labwc_version": ".".join(map(str, labwc)) if labwc else None,
        "linked_wlroots_version": ".".join(map(str, wlroots)) if wlroots else None,
        "minimum_upstream_versions": {"labwc": "0.20.0", "wlroots": "0.20.1"},
        "hdr_output_validated": False,
        "remaining_requirements": [
            "Confirm the running desktop uses this installed compositor build.",
            "Confirm wlroots Vulkan renderer is built, selected and working on Pi 5.",
            "Validate V4L2 HEVC Main10 SAND/dmabuf import into the browser and Vulkan presentation path.",
            "Validate genuine GFN 3840x2160 60fps HDR entitlement and stream color signaling.",
            "Validate browser color management, KMS/HDMI metadata and physical display HDR output.",
            "Main10 or decoded HDR metadata alone is not display HDR; reject tone-mapped SDR as proof.",
        ],
        "note": "Read-only prerequisite diagnosis, not a renderer probe, HDR enablement or acceptance pass.",
    }


def probe():
    try:
        completed = subprocess.run(
            ["/usr/bin/labwc", "--version"],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        result = assess("")
        result["probe_error"] = str(error)
        return result
    return assess(completed.stdout)


if __name__ == "__main__":
    print(json.dumps(probe(), indent=2))

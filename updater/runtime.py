"""Signed OTA orchestration and reboot reconciliation; no client-selected inputs."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

from .artifacts import (ArtifactError, GENERATED_PATHS, SemVer, sha256_file,
                        verify_bundle)
from .discovery import Discovery, DiscoveryError
from .platform import LinuxPlatform
from .state import Config, Journal, UpdateError, fail, read_json, trusted_path

COMMANDS = frozenset({"status", "check", "install", "cancel", "restart"})
DESTRUCTIVE = frozenset({"invalidating", "staging_boot", "staging_root",
                         "verifying_slot", "publishing"})
BUSY = DESTRUCTIVE | {"checking", "downloading", "verifying"}
ERRORS = (UpdateError, ArtifactError, DiscoveryError, OSError)


class Runtime:
    def __init__(self, config: Config, *, platform=None, discovery=None,
                 verifier=verify_bundle, clock=time.monotonic):
        self.config = config
        self.journal = Journal(Path(config.state_dir))
        self.platform = platform or LinuxPlatform(config)
        self.discovery = discovery or Discovery(
            config.repo, config.channel, platform=config.platform,
            cache_file=Path(config.state_dir) / "discovery.json")
        self.verifier, self.clock = verifier, clock
        self.cancelled = threading.Event()
        self._cancel_lock = threading.Lock()
        self._destructive = False

    def status(self):
        gate = None
        try:
            self.config.mutation_gate()
            if "boot/autoboot.txt" not in GENERATED_PATHS:
                fail("ARTIFACT_CONTRACT", "closed boot/autoboot.txt exemption is not installed")
        except UpdateError as exc:
            gate = dict(code=exc.code, message=str(exc))
        if not self.journal.path.exists():
            return dict(phase="uninitialized", install_enabled=False, gate=gate,
                        error=dict(code="UNINITIALIZED", message="Root provisioning/reconciliation required"))
        state = self.journal.load()
        pending, available = state.get("pending"), state.get("available")
        return dict(
            phase=state["phase"], current_version=state["current_version"],
            highest_version=state["highest_version"],
            available_version=available["version"] if available else None,
            candidate_version=pending["metadata"]["version"] if pending else None,
            notes=(available or {}).get("notes", "")[:2048],
            published_at=(available or {}).get("published_at"),
            download_size=(available or {}).get("size"),
            progress=state.get("progress"), error=state["error"], notice=state.get("notice"),
            install_enabled=gate is None and state["phase"] == "available",
            mutation_enabled=gate is None, gate=gate, strikes=state["strikes"],
            can_cancel=state["phase"] in ("downloading", "verifying") and not self._destructive,
            can_restart=gate is None and state["phase"] == "ready_to_restart",
            provider_launch_allowed=state["pending"] is None and state["phase"] not in BUSY | {"recovery_required"},
            last_successful_check=self.discovery.last_successful_check,
            next_check_at=self.discovery.next_check_at,
        )

    def _error(self, exc):
        error = dict(code=getattr(exc, "code", "IO"), message=str(exc)[:2048])
        if self.journal.path.exists():
            self.journal.update(phase="failed", error=error)
        return error

    def initialize(self):
        if os.name != "posix" or os.geteuid() != 0:
            fail("ROOT", "journal provisioning requires Linux root")
        self.platform.inspect()
        self.journal.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        trusted_path(self.journal.directory, directory=True)
        with self.journal.operation():
            layout = self.platform.inspect()
            release = read_json(Path(self.config.release_file))
            sentinel = read_json(Path("/boot/firmware/slot-valid.json"), 4096)
            if (sentinel.get("slot") != layout.active
                    or sentinel.get("version") != release.get("version")
                    or not re.fullmatch(r"[0-9a-f]{64}", sentinel.get("manifest_sha256", ""))):
                fail("IDENTITY", "initial release/sentinel does not match mounted slot")
            identity = dict(
                slot=layout.active, version=release["version"],
                manifest_sha256=sentinel["manifest_sha256"],
                config_sha256=sha256_file(Path("/boot/firmware/config.txt")))
            self.journal.initialize(release["version"], layout.active,
                                    self.config.minimum_key_epoch, identity=identity)
        return self.status()

    def check(self, *, force=True):
        self.platform.inspect()
        with self.journal.operation():
            state = self.journal.load()
            if state["pending"] or state["phase"] in BUSY:
                fail("BUSY", "candidate or unfinished operation requires reconciliation")
            self.journal.update(phase="checking", error=None)
            try:
                release = self.discovery.check(state["current_version"], force=force)
                if release and SemVer.parse(release["version"]) < SemVer.parse(state["highest_version"]):
                    fail("VERSION_FLOOR", "discovered release is below highest accepted version")
                self.journal.update(phase="available" if release else "idle",
                                    available=release, progress=None, error=None)
            except ERRORS as exc:
                self._error(exc)
                raise
        return self.status()

    def cancel(self):
        with self._cancel_lock:
            phase = self.journal.load()["phase"]
            if self._destructive or phase not in ("downloading", "verifying"):
                fail("CANCEL_TOO_LATE", "cancellation is only allowed before slot invalidation")
            self.cancelled.set()
        return self.status()

    def _cancel_check(self):
        if self.cancelled.is_set():
            fail("CANCELLED", "update cancelled before destructive staging")

    def install(self):
        self.config.mutation_gate()
        self.platform.inspect()
        with self.journal.operation(), self.platform.provider_lock():
            state = self.journal.load()
            if state["phase"] != "available" or not state.get("available") or state["pending"]:
                fail("STATE", "install requires server-selected available release")
            if state["strikes"] >= self.config.strike_limit:
                fail("STRIKE_LIMIT", "candidate failures require explicit operator retry")
            # This is an intentional blocker until the artifact owner grants the
            # exact mirror exemption; never mutate an ordinary signed file.
            if "boot/autoboot.txt" not in GENERATED_PATHS:
                fail("ARTIFACT_CONTRACT", "boot/autoboot.txt closed generated exemption is required")
            layout = self.platform.precheck()
            if layout.active != state["last_good"]:
                fail("SLOT", "not running the durable last-good slot")
            release = read_json(Path(self.config.release_file))
            trusted_path(Path(self.config.keys_dir), directory=True)
            if release.get("version") != state["current_version"]:
                fail("VERSION", "running release differs from durable current version")
            self.platform.verify_good(layout, state["last_good_identity"])
            self.cancelled.clear()
            self._destructive = False
            staging = Path(self.config.state_dir) / ("release-" + uuid.uuid4().hex)
            staging.mkdir(mode=0o700)
            downloads, extracted = staging / "download", staging / "verified"
            downloads.mkdir(mode=0o700)
            try:
                self.journal.update(phase="downloading", error=None, progress=None)
                last_progress = [0.0]
                def progress(received, total):
                    now = self.clock()
                    if now - last_progress[0] >= 1 or received == total:
                        self.journal.update(progress=dict(received=received, total=total))
                        last_progress[0] = now
                bundle, signature = self.discovery.download(
                    state["available"], downloads, progress=progress,
                    cancel=self.cancelled.is_set)
                self._cancel_check()
                self.journal.update(phase="verifying", progress=None)
                metadata = self.verifier(
                    bundle, signature, Path(self.config.keys_dir), extracted,
                    platform=self.config.platform, channel=self.config.channel,
                    current_version=state["current_version"],
                    highest_version=state["highest_version"],
                    minimum_key_epoch=max(state["minimum_key_epoch"], self.config.minimum_key_epoch),
                    data_schema=self.config.data_schema)
                # SemVer equality ignores build metadata; release identity must not.
                if metadata["version"] != state["available"]["version"]:
                    fail("RELEASE_IDENTITY", "signed version differs from selected release tag")
                pending = dict(slot=layout.target, metadata=metadata,
                               generated=None, attempted=False, boot_id=None,
                               rollback_attempted=False,
                               previous=state["last_good_identity"])
                with self._cancel_lock:
                    self._cancel_check()
                    self._destructive = True
                    self.journal.update(
                        phase="invalidating", pending=pending,
                        highest_version=metadata["version"],
                        minimum_key_epoch=max(state["minimum_key_epoch"], metadata["key_epoch"]))
                with self.platform.inhibitor() as inhibitor_alive:
                    started = self.clock()
                    def checkpoint(phase, generated):
                        if not inhibitor_alive() or self.clock() - started >= 1500:
                            fail("STAGING_DEADLINE", "staging inhibitor/deadline expired")
                        if generated is not None:
                            pending["generated"] = generated
                        self.journal.update(phase=phase, pending=copy.deepcopy(pending))
                    self.platform.stage(extracted, metadata, layout, checkpoint)
            except ERRORS as exc:
                self._error(exc)
                raise
            finally:
                # The path is exclusively generated under our private staging root.
                # Never delete recovered/unknown directories automatically.
                shutil.rmtree(staging)
        return self.status()

    def restart(self):
        self.config.mutation_gate()
        self.platform.inspect()
        with self.journal.operation(), self.platform.provider_lock():
            state = self.journal.load()
            if state["phase"] != "ready_to_restart" or not state["pending"]:
                fail("STATE", "restart is only valid for a verified staged candidate")
            layout = self.platform.inspect()
            pending = state["pending"]
            if layout.active != state["last_good"] or layout.target != pending["slot"]:
                fail("SLOT", "active/candidate relationship changed")
            self.platform.verify_good(layout, state["last_good_identity"])
            self.platform.verify_candidate(layout, pending)
            self.platform.write_pointers(layout, layout.active, pending["slot"])
            pending["attempted"] = True
            self.journal.update(pending=pending)
            self.platform.reboot(tryboot=True)
        return self.status()

    def reconcile(self, *, boot_id=None):
        self.config.mutation_gate()
        with self.journal.operation():
            state = self.journal.load()
            layout = self.platform.inspect()
            pending = state["pending"]
            if pending is None:
                if layout.active != state["last_good"]:
                    fail("RECOVERY", "unexpected running slot; refusing silent adoption")
                if state["phase"] in BUSY:
                    self.journal.update(phase="failed", error=dict(
                        code="INTERRUPTED", message="Non-destructive operation interrupted; check again"))
                return self.status()
            if pending["slot"] not in ("A", "B") or pending["slot"] == state["last_good"]:
                fail("STATE", "invalid pending slot relationship")
            if layout.active == pending["slot"]:
                guard = self.platform.guard_state(pending)
                if guard["attempted"]:
                    self.journal.update(phase="recovery_required", error=dict(
                        code="ROLLBACK_LOOP", message="Boot-local rollback already attempted; operator recovery required"))
                    fail("ROLLBACK_LOOP", "refusing a second boot-local rollback")
                if state["phase"] == "promoting" and guard["confirmed"]:
                    self._commit_promotion(state)
                    return self.status()
                if self.clock() >= guard["deadline_seconds"]:
                    return self._rollback_locked(state, layout, "Candidate exhausted its boot-local deadline")
                if state["phase"] not in ("publishing", "ready_to_restart", "tryboot_running", "promoting"):
                    return self._rollback_locked(state, layout, "Incomplete candidate booted")
                boot_id = boot_id or self.platform.boot_id()
                if pending.get("boot_id") not in (None, boot_id):
                    return self._rollback_locked(state, layout, "Candidate rebooted without confirmation")
                if pending.get("boot_id") is None:
                    pending.update(boot_id=boot_id, started=self.clock(),
                                   deadline=guard["deadline_seconds"],
                                   healthy_since=None, last_health_check=None)
                # Durable pending identity wins even when firmware DT tryboot=0.
                self.journal.update(phase="tryboot_running", pending=pending, error=None)
            elif layout.active == state["last_good"]:
                attempted = bool(pending.get("attempted") or pending.get("boot_id")
                                 or pending.get("rollback_attempted"))
                if not attempted and state["phase"] in ("ready_to_restart", "publishing"):
                    attempted = self.platform.candidate_guard_attempted(layout, pending)
                if attempted:
                    graceful = (pending.get("boot_id") is not None
                                and pending.get("graceful_shutdown") == pending["boot_id"]
                                and not pending.get("rollback_attempted")
                                and not self.platform.candidate_guard_attempted(layout, pending))
                    self.journal.update(
                        phase="rolled_back", pending=None, strikes=state["strikes"] + (0 if graceful else 1),
                        error=None, notice="Update failed; returned to the previous version")
                elif state["phase"] in ("ready_to_restart", "publishing"):
                    try:
                        self.platform.verify_candidate(layout, pending)
                        self.journal.update(phase="ready_to_restart")
                    except ERRORS as exc:
                        self._error(exc)
                        raise
                else:
                    self.journal.update(phase="failed", pending=None, error=dict(
                        code="INTERRUPTED", message="Inactive slot invalidated; download/install again"))
            else:
                fail("RECOVERY", "unknown running slot")
        return self.status()

    def shutdown(self):
        """Explicit system-shutdown hook, never inferred from daemon termination."""
        self.config.mutation_gate()
        with self.journal.operation():
            state = self.journal.load()
            pending = state["pending"]
            if state["phase"] == "tryboot_running" and pending and pending.get("boot_id"):
                pending["graceful_shutdown"] = pending["boot_id"]
                self.journal.update(pending=pending)
        return self.status()

    def retry(self):
        self.config.mutation_gate()
        with self.journal.operation():
            state = self.journal.load()
            layout = self.platform.inspect()
            if state["pending"] or layout.active != state["last_good"]:
                fail("RECOVERY", "retry requires the reconciled last-good slot")
            self.platform.verify_good(layout, state["last_good_identity"])
            self.journal.update(strikes=0, phase="idle", error=None)
        return self.status()

    def rollback(self):
        self.config.mutation_gate()
        with self.journal.operation():
            state = self.journal.load()
            layout = self.platform.inspect()
            if not state["pending"] or layout.active != state["pending"]["slot"]:
                fail("RECOVERY", "rollback requires a running durable candidate")
            return self._rollback_locked(state, layout, "Explicit root-operator rollback")

    def _rollback_locked(self, state, layout, reason):
        pending = state["pending"]
        if layout.active != pending["slot"] or layout.active == state["last_good"]:
            fail("RECOVERY", "refusing to quarantine last-good; reconcile the running slot first")
        with self.platform.guard_lock():
            if pending.get("rollback_attempted") or self.platform.guard_state(pending)["attempted"]:
                self.journal.update(phase="recovery_required", error=dict(
                    code="ROLLBACK_LOOP", message="Last-good boot failed; refusing another reboot"))
                fail("ROLLBACK_LOOP", "operator recovery required; automatic reboot already attempted")
            self.platform.verify_good(layout, state["last_good_identity"])
            pending["rollback_attempted"] = True
            self.journal.update(phase="recovery_required", pending=pending,
                                error=dict(code="ROLLBACK", message=reason))
            self.platform.mark_guard()
            # Firmware ignores mirrors if control disappears. Quarantine first.
            self.platform.quarantine_running()
            self.platform.write_pointers(layout, state["last_good"], state["last_good"])
            self.platform.reboot()
        return self.status()

    def _commit_promotion(self, state):
        pending = state["pending"]
        identity = dict(slot=pending["slot"], version=pending["metadata"]["version"],
                        manifest_sha256=pending["metadata"]["manifest_sha256"],
                        config_sha256=pending["metadata"]["manifest"]["boot/config.txt"]["sha256"])
        self.journal.update(phase="promoted", pending=None,
                            current_version=identity["version"], last_good=identity["slot"],
                            last_good_identity=identity, strikes=0, available=None,
                            error=None, notice="Update confirmed successfully")

    def health(self, *, deadline_only=False):
        self.config.mutation_gate()
        # The daemon's deadline probe shares this lock with the health timer.
        # Wait through brief contention rather than dropping a health sample.
        with self.journal.operation(timeout=5 if not deadline_only else 0):
            state = self.journal.load()
            if state["phase"] not in ("tryboot_running", "promoting") or not state["pending"]:
                return self.status()
            pending = state["pending"]
            guard = self.platform.guard_state(pending)
            if guard["attempted"]:
                self.journal.update(phase="recovery_required", error=dict(
                    code="ROLLBACK_LOOP", message="Boot-local rollback already attempted"))
                fail("ROLLBACK_LOOP", "operator recovery required; early rollback already attempted")
            if state["phase"] == "promoting" and guard["confirmed"]:
                if self.platform.inspect().active != pending["slot"]:
                    fail("RECOVERY", "confirmed promotion is not the running slot")
                self._commit_promotion(state)
                return self.status()
            now = self.clock()
            if self.platform.boot_id() != pending["boot_id"]:
                return self._rollback_locked(state, self.platform.inspect(),
                                             "Candidate boot changed without confirmation")
            if now >= pending["deadline"]:
                return self._rollback_locked(state, self.platform.inspect(), "Candidate confirmation deadline expired")
            if deadline_only:
                return self.status()
            try:
                layout = self.platform.health(pending)
            except ERRORS as exc:
                if self.clock() >= pending["deadline"]:
                    return self._rollback_locked(state, self.platform.inspect(),
                                                 "Failed health checks exhausted candidate deadline")
                pending["healthy_since"] = None
                pending["last_health_check"] = None
                self.journal.update(pending=pending,
                                    error=dict(code=getattr(exc, "code", "HEALTH"), message=str(exc)))
                return self.status()
            now = self.clock()
            if now >= pending["deadline"]:
                return self._rollback_locked(state, layout, "Health checks exceeded candidate deadline")
            last_check = pending.get("last_health_check")
            pending["last_health_check"] = now
            if (pending.get("healthy_since") is None or last_check is None
                    or now - last_check > 30):
                pending["healthy_since"] = now
                self.journal.update(pending=pending, error=None)
            elif now - pending["healthy_since"] >= self.config.stabilization_seconds:
                with self.platform.guard_lock():
                    if self.platform.guard_state(pending)["attempted"]:
                        fail("ROLLBACK_LOOP", "early rollback claimed the candidate before promotion")
                    self.journal.update(phase="promoting", pending=pending)
                    self.platform.write_pointers(layout, pending["slot"], state["last_good"])
                    self.platform.mark_guard(confirmed=True)
                    self._commit_promotion(state)
            else:
                self.journal.update(pending=pending, error=None)
        return self.status()

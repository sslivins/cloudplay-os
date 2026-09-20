#!/usr/bin/env python3
"""Headless Wayland smoke test of the real native UI, with no service/network login."""
import os
import sys
from pathlib import Path

source = Path(__file__).resolve().parents[1] / "launcher"
sys.path.insert(0, str(source if source.is_dir() else Path("/usr/local/lib/cloudplay/launcher")))
import main
from updates import Updates as UpdateClient
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk


class Browser:
    def __init__(self):
        self.service = None
        self.starts = []
        self.stops = 0
        self.fail_stop = False
        self.crashed = False

    def start(self, service):
        self.stop()
        self.starts.append(service)
        self.service = service

    def stop(self):
        if self.fail_stop:
            raise RuntimeError("test stop failure")
        self.stops += 1
        self.service = None

    def exited(self):
        return self.crashed and self.service is not None


class Control:
    pending = False

    def poll(self):
        pending, self.pending = self.pending, False
        return pending

    def close(self):
        pass


class Pads:
    actions = []

    def poll(self, visible):
        actions, self.actions = self.actions, []
        return actions

    def close(self):
        pass


browser, control, pads = Browser(), Control(), Pads()
step = 0
errors = []
done = False
update_mode = os.environ.get("CLOUDPLAY_SMOKE_UPDATES") == "1"
trusted_mode = os.environ.get("CLOUDPLAY_SMOKE_TRUSTED") == "1"
progress_widgets = {}
progress_pulses = []
progress_unmaps = []


class Updates:
    display_status = UpdateClient.display_status

    def __init__(self):
        self.status = {"phase": "available", "install_enabled": True,
                       "provider_launch_allowed": True,
                       "requires_trusted_session": not trusted_mode,
                       "channel": "beta", "channel_change_enabled": trusted_mode,
                       "current_version": "0.1.0-beta.3", "available_version": "0.1.0-beta.4"}
        self.error = ""
        self.changed = False
        self.commands = []
        self.active_command = None
        self.queued_action = None
        self.fail_close = False
        self.initial_status = dict(self.status) if trusted_mode else None
        if trusted_mode:
            self.status = {"phase": "unknown"}
        self.check_reply = False
        # Exercise replies later than the driver's first 150 ms assertion tick.
        self.initial_status_polls = 4 if trusted_mode else 0
        self.initial_check_polls = 4 if trusted_mode else 0

    def pending(self):
        return self.initial_status is not None or self.check_reply or self.changed

    def submit(self, command):
        self.commands.append(command)
        if command == "check":
            self.active_command = command
            self.check_reply = True
            self.changed = True
            return True
        if command == "install":
            self.active_command = command
            self.changed = True
            return True
        if command == "close" and self.fail_close:
            self.error = "Unable to return to the Main Menu."
            self.changed = True
            return True
        if command in ("enable_beta", "disable_beta"):
            self.status["channel"] = "beta" if command == "enable_beta" else "stable"
        self.status["phase"] = {"install": "restarting", "restart": "restarting",
                                "dismiss": "idle"}.get(command, "available")
        self.status["can_restart"] = self.status["phase"] == "ready_to_restart"
        self.status["provider_launch_allowed"] = self.status["phase"] in ("idle", "available")
        self.changed = True
        return True

    def poll(self):
        if self.initial_status is not None:
            if self.initial_status_polls:
                self.initial_status_polls -= 1
                return False
            self.status, self.initial_status = self.initial_status, None
            self.changed = True
        elif self.check_reply:
            if self.initial_check_polls:
                self.initial_check_polls -= 1
                return False
            self.check_reply = False
            self.active_command = None
            self.changed = True
        changed, self.changed = self.changed, False
        return changed

    def close(self):
        pass


updates = Updates() if update_mode or trusted_mode else None
heartbeats = None
if trusted_mode:
    from heartbeat import Heartbeats
    heartbeats = Heartbeats(Path(os.environ["XDG_RUNTIME_DIR"]))


def press(window, keyval):
    event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
    event.keyval = keyval
    assert window.emit("key-press-event", event), "Navigation key was not handled"


def children_of(window):
    overlay = window.get_child()
    return overlay.get_child().get_children() + [
        widget for widget in overlay.get_children() if widget is not overlay.get_child()]


def snapshot(window, name):
    directory = os.environ.get("CLOUDPLAY_SMOKE_SCREENSHOTS")
    if not directory:
        return
    import cairo
    content = window.get_child().get_child()
    monitor = Gdk.Display.get_default().get_monitor_at_window(window.get_window())
    geometry = monitor.get_geometry()
    assert window.get_allocated_height() <= geometry.height, "Window exceeds physical display height"
    assert window.get_allocated_width() <= geometry.width, "Window exceeds physical display width"
    assert content.get_allocated_height() <= window.get_allocated_height(), "Content exceeds display height"
    assert content.get_allocated_width() <= window.get_allocated_width(), "Content exceeds display width"
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, window.get_allocated_width(),
                                 window.get_allocated_height())
    window.draw(cairo.Context(surface))
    surface.write_to_png(str(Path(directory) / (name + ".png")))


def check_timeline(window, active, *, moving=True):
    grid, = [w for w in children_of(window) if isinstance(w, Gtk.Grid)]
    labels = ("Download", "Prepare", "Install", "Check", "Restart")
    for index, name in enumerate(labels):
        marker = grid.get_child_at(index * 2, 0)
        label = grid.get_child_at(index * 2, 1)
        state = "done" if index < active else "active" if index == active else "upcoming"
        assert label.get_text() == name and label.get_style_context().has_class(state)
        assert marker.get_accessible().get_name() == f"{name}: {state}"
        spinner, = [w for w in marker.get_child().get_children() if isinstance(w, Gtk.Spinner)]
        assert spinner.get_property("active") == (moving and index == active)
    header, = [w for w in children_of(window) if w.get_style_context().has_class("update-version")]
    assert header.get_allocation().y < grid.get_allocation().y
    return grid, header


def drive_trusted(window, buttons, titles):
    global done
    assert not browser.starts, "Trusted session started a browser"
    if step == 0:
        assert "SYSTEM UPDATES" in titles and len(buttons) == 3
        buttons[0].grab_focus()
        press(window, Gdk.KEY_Down)
        press(window, Gdk.KEY_Return)
    elif step == 1:
        assert "CONFIRM UPDATE" in titles and window.get_focus() == buttons[0]
        assert any("restart automatically" in text for text in titles)
        assert any("Your Cloudplay device will restart automatically when the update is complete."
                   in text for text in titles)
        assert any("Do not disconnect from power." in text for text in titles)
        assert not any("ask you to restart" in text for text in titles)
        snapshot(window, "update-confirmation")
        assert updates.commands == ["check"]
        press(window, Gdk.KEY_space)
    elif step == 2:
        assert "SYSTEM UPDATES" in titles
        assert updates.commands == ["check"]
        press(window, Gdk.KEY_Down)
        press(window, Gdk.KEY_KP_Enter)
        confirmation = [w for w in children_of(window) if isinstance(w, Gtk.Button)]
        assert window.get_focus() == confirmation[0]
        press(window, Gdk.KEY_Down)
        press(window, Gdk.KEY_space)
    elif step == 3:
        assert updates.commands == ["check", "install"] and not buttons
        assert "SYSTEM UPDATES" in titles and "CONFIRM UPDATE" not in titles
        assert updates.status["phase"] == "available"
        assert "Starting your update..." in titles
        assert not any("is available" in text for text in titles)
        assert "Do not disconnect from power." in titles
        for key in (Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_Left, Gdk.KEY_Right,
                    Gdk.KEY_Return, Gdk.KEY_space, Gdk.KEY_Escape):
            press(window, key)
        pads.actions = ["up", "down", "left", "right", "accept", "back"]
    elif step == 4:
        updates.active_command = None
        updates.status.update(phase="restarting", can_restart=False, provider_launch_allowed=False)
        updates.changed = True
    elif step == 5:
        assert "SYSTEM UPDATES" in titles and not buttons
        assert updates.commands == ["check", "install"]
        check_timeline(window, 3)
        updates.status.update(operation=dict(name="save_restart"))
        updates.changed = True
    elif step == 6:
        check_timeline(window, 4)
        updates.status.update(phase="staging_root", provider_launch_allowed=False, operation=None,
                              progress={"received": 5 * 1024**2, "total": 20 * 1024**2})
        updates.changed = True
    elif step == 7:
        bar, = [w for w in children_of(window) if isinstance(w, Gtk.ProgressBar)]
        assert bar.get_fraction() == 0.25 and bar.get_text() == "25%"
        assert not buttons and window.get_focus() is None
        snapshot(window, "update-copy-progress")
        grid, header = check_timeline(window, 2)
        progress_widgets.update(bar=bar, grid=grid, header=header,
                                header_y=header.get_allocation().y)
        window.connect("unmap", lambda *_: progress_unmaps.append(True))
        updates.status["progress"]["received"] = 10 * 1024**2
        updates.changed = True
    elif step == 8:
        bar, = [w for w in children_of(window) if isinstance(w, Gtk.ProgressBar)]
        assert bar is progress_widgets["bar"] and bar.get_fraction() == 0.5
        assert not buttons and window.get_focus() is None
        assert not progress_unmaps, "A progress refresh remapped the window"
        grid, header = check_timeline(window, 2)
        assert grid is progress_widgets["grid"] and header is progress_widgets["header"]
        assert header.get_allocation().y == progress_widgets["header_y"]
        updates.status.update(phase="verifying_slot", progress=None)
        updates.changed = True
    elif step == 9:
        bar, = [w for w in children_of(window) if isinstance(w, Gtk.ProgressBar)]
        assert bar is progress_widgets["bar"] and not bar.get_visible()
        assert any("Checking installed files" in text for text in titles)
        check_timeline(window, 3)
        original_pulse = bar.pulse
        def pulse():
            progress_pulses.append(True)
            original_pulse()
        bar.pulse = pulse
    elif step == 10:
        assert not progress_pulses, "Unknown work must not pulse a progress-shaped bar"
        updates.status["operation"] = dict(name="check_installed", received=30 * 1024**2,
                                          total=100 * 1024**2, elapsed=90)
        updates.changed = True
    elif step == 11:
        bar = progress_widgets["bar"]
        assert bar.get_visible() and bar.get_fraction() == 0.3
        assert bar.get_text() == "30%"
        assert not buttons and window.get_focus() is None
        snapshot(window, "update-file-check")
        updates.status["operation"] = dict(name="save_system", elapsed=65)
        updates.changed = True
    elif step == 12:
        assert not progress_widgets["bar"].get_visible()
        assert not any("Elapsed:" in text or "Waiting for progress" in text for text in titles)
        assert not progress_widgets["bar"].get_visible()
        assert any("Saving system files" in text for text in titles)
        check_timeline(window, 3)
        snapshot(window, "update-storage-wait")
        assert not progress_unmaps, "Operation changes remapped the update screen"
        updates.error = "Update status connection unavailable"
        updates.changed = True
    elif step == 13:
        assert not any(isinstance(w, Gtk.ProgressBar) for w in children_of(window))
        assert updates.error in titles
        check_timeline(window, 3, moving=False)
        updates.error = ""
        updates.status.update(phase="promoted", provider_launch_allowed=True)
        updates.changed = True
    elif step == 14:
        assert len(buttons) == 2
        assert not any(isinstance(w, Gtk.Grid) for w in children_of(window))
        snapshot(window, "update-complete")
        updates.status.update(phase="verifying", provider_launch_allowed=False, can_cancel=True,
                              operation=dict(name="unpack", received=45, total=100))
        updates.changed = True
    elif step == 15:
        check_timeline(window, 1)
        assert "Unpacking update files" in titles
        bar, = [w for w in children_of(window) if isinstance(w, Gtk.ProgressBar)]
        assert bar.get_text() == "45%"
        snapshot(window, "update-unpacking")
        assert len(buttons) == 1
        assert any(isinstance(w, Gtk.Label) and w.get_text() == "Cancel Update"
                   for w in buttons[0].get_child().get_children())
        press(window, Gdk.KEY_Return)
        assert updates.commands == ["check", "install", "cancel"]
        updates.status.update(phase="ready_to_restart", can_restart=True, can_cancel=False,
                              provider_launch_allowed=False, operation=None)
        updates.changed = True
    elif step == 16:
        check_timeline(window, 4, moving=False)
        snapshot(window, "update-ready")
        buttons[-1].clicked()
        assert updates.commands == ["check", "install", "cancel", "restart"]
        check_timeline(window, 3)
        assert not any(isinstance(w, Gtk.Label) and w.get_text() == "CONFIRM UPDATE"
                       for w in children_of(window)), "Recovery restart must not ask for confirmation twice"
        updates.status.update(phase="promoted", can_restart=False, provider_launch_allowed=True)
        updates.changed = True
    elif step == 17:
        for name in ("launcher", "compositor"):
            path = Path(os.environ["XDG_RUNTIME_DIR"]) / (name + "-heartbeat.json")
            assert path.is_file(), "Missing real Wayland/GTK heartbeat: " + name
        buttons[-1].clicked()
    elif step == 18:
        assert "SETTINGS" in titles and len(buttons) == 3
        updates.status.update(current_version="0.1.0-beta.4", available_version=None)
        buttons[0].clicked()
        assert updates.commands == ["check", "install", "cancel", "restart", "check"]
        spinner, = [w for w in children_of(window) if isinstance(w, Gtk.Spinner)]
        assert spinner.get_property("active")
        assert not any(isinstance(w, Gtk.ProgressBar) for w in children_of(window))
        assert not any(isinstance(w, Gtk.Label) and "Do not disconnect" in w.get_text()
                       for w in children_of(window))
        assert any(isinstance(w, Gtk.Label) and w.get_text() == "Cloudplay OS 0.1.0-beta.4"
                   for w in children_of(window))
        assert not any(isinstance(w, Gtk.Grid) for w in children_of(window))
        assert any(isinstance(w, Gtk.Label) and w.get_text() == "Checking for updates..."
                   for w in children_of(window))
        assert not any(isinstance(w, Gtk.Label) and "Update complete" in w.get_text()
                       for w in children_of(window))
        press(window, Gdk.KEY_Escape)
        settings_buttons = [w for w in children_of(window) if isinstance(w, Gtk.Button)]
        settings_buttons[1].clicked()
    elif step == 19:
        assert "BETA RELEASES" in titles and len(buttons) == 2
        assert any("Beta releases: On" in text for text in titles)
        snapshot(window, "beta-releases-on")
        buttons[0].clicked()
    elif step == 20:
        assert any("Beta releases: Off" in text for text in titles)
        assert updates.commands == ["check", "install", "cancel", "restart", "check", "disable_beta"]
        snapshot(window, "beta-releases-off")
        buttons[0].clicked()
    elif step == 21:
        assert any("Beta releases: On" in text for text in titles)
        updates.status.update(channel_change_enabled=False)
        updates.changed = True
    elif step == 22:
        assert len(buttons) == 1
        assert any("Finish the current update" in text for text in titles)
        updates.status.update(channel_change_enabled=True)
        updates.changed = True
    elif step == 23:
        buttons[-1].clicked()
    elif step == 24:
        assert "SETTINGS" in titles
        updates.status.update(phase="tryboot_running", provider_launch_allowed=False,
                              error=dict(code="HEALTH_NOT_READY", message="startup service is still starting"))
        buttons[0].clicked()
    elif step == 25:
        assert updates.commands.count("check") == 2
        assert not buttons
        assert "Finishing your update..." in titles
        assert not any("problem continues" in text or "Reference:" in text for text in titles)
        assert "Waiting for progress..." not in titles
        assert not any(isinstance(w, Gtk.Grid) for w in children_of(window))
        header, = [w for w in children_of(window) if w.get_style_context().has_class("update-version")]
        assert not header.get_visible()
        assert "close" not in updates.commands
        snapshot(window, "update-postboot-finishing")
        updates.status.update(phase="promoting", error=None)
        updates.changed = True
    elif step == 26:
        assert "Finishing your update..." in titles and not buttons
        assert "close" not in updates.commands
        updates.status.update(phase="promoted", provider_launch_allowed=False)
        updates.changed = True
    elif step == 27:
        assert "close" not in updates.commands
        updates.fail_close = True
        updates.status.update(provider_launch_allowed=True)
        updates.changed = True
    elif step == 28:
        assert updates.error in titles
        assert updates.commands.count("close") == 1
        updates.changed = True
    elif step == 29:
        assert updates.commands == ["check", "install", "cancel", "restart", "check",
                                    "disable_beta", "enable_beta", "close"]
        buttons[-1].clicked()
    elif step == 30:
        assert "SETTINGS" in titles
        updates.fail_close = False
        buttons[-1].clicked()
        assert updates.commands.count("close") == 2
        done = True
        Gtk.main_quit()
        return False
    return True


def drive_updates(window, children, buttons, titles):
    global done
    if step == 0:
        assert "MAIN MENU" in titles and len(buttons) == 4
        snapshot(window, "main-menu-updates")
        assert buttons[2].get_label() == "Settings"
        assert "new version available" in buttons[3].get_label()
        assert window.get_focus() is buttons[0], "Updates stole initial gaming focus"
        assert buttons[3].get_valign() == Gtk.Align.START
        buttons[1].grab_focus()
        press(window, Gdk.KEY_Up)
        assert window.get_focus() is buttons[3]
        press(window, Gdk.KEY_Down)
        assert window.get_focus() is buttons[1], "Down did not return to the previous provider"
        press(window, Gdk.KEY_Up)
        press(window, Gdk.KEY_Left)
        assert window.get_focus() is buttons[2]
        press(window, Gdk.KEY_Return)
    elif step == 1:
        assert "SETTINGS" in titles and len(buttons) == 3
        snapshot(window, "settings")
        assert window.get_focus() is buttons[0]
        buttons[0].clicked()
        assert updates.commands == ["open"], "Settings must open the isolated updater directly"
    elif step == 2:
        assert "SYSTEM UPDATES" in titles and len(buttons) == 1
        assert not any("browser-free" in text or "Open Update Controls" in text for text in titles)
        press(window, Gdk.KEY_Escape)
    elif step == 3:
        assert "SETTINGS" in titles
        press(window, Gdk.KEY_Escape)
    elif step == 4:
        assert "MAIN MENU" in titles
        buttons[3].clicked()
        assert updates.commands == ["open", "open"], "Bell must open the same isolated updater"
        updates.error = "System Updates could not be opened"
        updates.changed = True
    elif step == 5:
        assert "SYSTEM UPDATES" in titles and len(buttons) == 2
        assert updates.error in titles
        buttons[0].clicked()
        assert updates.commands == ["open", "open", "open"]
        updates.error = ""
        updates.changed = True
    elif step == 6:
        press(window, Gdk.KEY_Escape)
    elif step == 7:
        assert "SETTINGS" in titles
        control.pending = True  # Home shortcut also works from Settings.
    elif step == 8:
        assert "MAIN MENU" in titles
        buttons[3].grab_focus()
        updates.status.update(phase="idle", last_successful_check=10)
        updates.changed = True
    elif step == 9:
        visible = [button for button in buttons if button.get_visible()]
        assert len(visible) == 3, "Bell should be hidden without a notification"
        assert window.get_focus() is buttons[2], "Disappearing notification lost keyboard focus"
        snapshot(window, "main-menu-no-updates")
        press(window, Gdk.KEY_Down)
        assert window.get_focus() is buttons[1]
        updates.status.update(phase="available")
        updates.changed = True
    elif step == 10:
        assert len([button for button in buttons if button.get_visible()]) == 4
        assert window.get_focus() is buttons[1], "New notification stole focus"
        updates.status.update(phase="idle")
        updates.changed = True
    elif step == 11:
        buttons[2].clicked()
    elif step == 12:
        assert "SETTINGS" in titles, "Settings must remain available without a notification"
        buttons[0].clicked()
        assert updates.commands == ["open"] * 4
    elif step == 13:
        press(window, Gdk.KEY_Escape)
    elif step == 14:
        assert "SETTINGS" in titles
        buttons[1].clicked()
        assert updates.commands == ["open"] * 4 + ["open-beta"]
    elif step == 15:
        assert "BETA RELEASES" in titles and len(buttons) == 1
        assert not browser.starts, "Settings navigation must not start a game"
        done = True
        Gtk.main_quit()
        return False
    return True


def drive():
    global step, done
    try:
        if updates is not None and updates.pending():
            return True
        window, = [w for w in Gtk.Window.list_toplevels() if w.get_title() == "Cloudplay Home"]
        children = children_of(window)
        titles = [w.get_text() for w in children if isinstance(w, Gtk.Label)]
        title = next((text for text in titles if text in (
            "MAIN MENU", "GeForce NOW", "Xbox Cloud Gaming",
            "Could not return to the Main Menu")), "")
        buttons = [w for w in children if isinstance(w, Gtk.Button)]
        if trusted_mode:
            keep_running = drive_trusted(window, buttons, titles)
            step += 1
            return keep_running
        if update_mode:
            keep_running = drive_updates(window, children, buttons, titles)
            step += 1
            return keep_running
        if step == 0:
            assert title == "MAIN MENU" and len(buttons) == 3
            assert window.get_mapped()
            buttons[0].clicked()
            assert browser.service == "gfn" and not window.get_visible()
            control.pending = True
        elif step == 1:
            assert title == "GeForce NOW" and window.get_mapped()
            assert window.get_focus() == buttons[0]
            assert browser.service == "gfn" and browser.stops == 2
            buttons[0].clicked()  # Default is Stay, never an accidental leave.
            assert browser.service == "gfn" and not window.get_visible()
            control.pending = True
        elif step == 2:
            buttons[1].clicked()
            assert browser.starts == ["gfn", "gfn"]
            assert not window.get_visible()
            control.pending = True
        elif step == 3:
            browser.fail_stop = True
            buttons[2].clicked()
            assert browser.service == "gfn" and window.get_visible()
            pads.actions = ["back"]  # Stop failure must not be dismissible to an empty screen.
        elif step == 4:
            assert title == "Could not return to the Main Menu" and len(buttons) == 1
            assert window.get_visible()
            browser.fail_stop = False
            buttons[0].clicked()
            assert browser.service is None and window.get_visible()
            pads.actions = ["down", "accept"]
        elif step == 5:
            assert browser.service == "xbox" and not window.get_visible()
            pads.actions = ["home"]
        elif step == 6:
            assert title == "Xbox Cloud Gaming"
            assert browser.service == "xbox" and window.get_visible()
            pads.actions = ["back"]
        elif step == 7:
            assert not window.get_visible() and browser.service == "xbox"
            browser.crashed = True
        elif step == 8:
            if title != "MAIN MENU":
                return True
            assert browser.service is None and window.get_visible()
            buttons[2].clicked()
        elif step == 9:
            assert "SETTINGS" in titles
            buttons[0].clicked()
        elif step == 10:
            assert "SYSTEM UPDATES" in titles
            assert "Updates aren't available on this installation." in titles
            press(window, Gdk.KEY_Escape)
        elif step == 11:
            assert "SETTINGS" in titles
            done = True
            Gtk.main_quit()
            return False
        step += 1
        return True
    except BaseException as error:
        error.add_note(f"Native smoke step={step}, trusted={trusted_mode}, updates={update_mode}")
        errors.append(error)
        Gtk.main_quit()
        return False


def timeout():
    state = (f"phase={updates.status.get('phase')}, "
             f"initial_status_pending={updates.initial_status is not None}, "
             f"check_reply_pending={updates.check_reply}, changed={updates.changed}"
             if updates is not None else "no update client")
    errors.append(RuntimeError(
        f"Native UI smoke test timed out at step {step}: {state}"))
    Gtk.main_quit()
    return False


GLib.timeout_add(150, drive)
GLib.timeout_add_seconds(12, timeout)
main.run(browser, control, pads, updates, trusted_updates=trusted_mode, heartbeats=heartbeats)
if errors:
    raise errors[0]
assert done and browser.service is None
if os.environ.get("CLOUDPLAY_SMOKE_RESULT"):
    Path(os.environ["CLOUDPLAY_SMOKE_RESULT"]).write_text('{"passed": true}\n')
print("Native Home smoke passed: GFN/Xbox navigation, confirmation, stay, reload, stop failure, crash recovery")

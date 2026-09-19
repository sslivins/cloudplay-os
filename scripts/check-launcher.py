#!/usr/bin/env python3
"""Headless Wayland smoke test of the real native UI, with no service/network login."""
import os
import sys
from pathlib import Path

source = Path(__file__).resolve().parents[1] / "launcher"
sys.path.insert(0, str(source if source.is_dir() else Path("/usr/local/lib/cloudplay/launcher")))
import main
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
    def __init__(self):
        self.status = {"phase": "available", "install_enabled": True,
                       "provider_launch_allowed": True,
                       "current_version": "0.1.0-beta.3", "available_version": "0.1.0-beta.4"}
        self.error = ""
        self.changed = False
        self.commands = []

    def submit(self, command):
        self.commands.append(command)
        self.status["phase"] = {"install": "ready_to_restart", "dismiss": "idle"}.get(command, "available")
        self.status["can_restart"] = self.status["phase"] == "ready_to_restart"
        self.status["provider_launch_allowed"] = self.status["phase"] in ("idle", "available")
        self.changed = True
        return True

    def poll(self):
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
        press(window, Gdk.KEY_Down)
        press(window, Gdk.KEY_Return)
    elif step == 1:
        assert "CONFIRM UPDATE" in titles and window.get_focus() == buttons[0]
        snapshot(window, "update-confirmation")
        assert updates.commands == []
        press(window, Gdk.KEY_space)
    elif step == 2:
        assert "SYSTEM UPDATES" in titles
        assert updates.commands == []
        press(window, Gdk.KEY_Down)
        press(window, Gdk.KEY_KP_Enter)
        confirmation = [w for w in children_of(window) if isinstance(w, Gtk.Button)]
        assert window.get_focus() == confirmation[0]
        press(window, Gdk.KEY_Down)
        press(window, Gdk.KEY_space)
    elif step == 3:
        assert updates.commands == ["install"] and len(buttons) == 2
        buttons[1].grab_focus()
        press(window, Gdk.KEY_Return)
        confirmation = [w for w in children_of(window) if isinstance(w, Gtk.Button)]
        assert window.get_focus() == confirmation[0]
        press(window, Gdk.KEY_KP_Enter)
    elif step == 4:
        updates.status.update(phase="tryboot_running", can_restart=False)
        updates.changed = True
    elif step == 5:
        assert "SYSTEM UPDATES" in titles and len(buttons) == 1
        updates.status.update(phase="promoted", provider_launch_allowed=True)
        updates.changed = True
    elif step == 6:
        updates.status.update(phase="staging_root", provider_launch_allowed=False,
                              progress={"received": 5 * 1024**2, "total": 20 * 1024**2})
        updates.changed = True
    elif step == 7:
        bar, = [w for w in children_of(window) if isinstance(w, Gtk.ProgressBar)]
        assert bar.get_fraction() == 0.25 and bar.get_text() == "25%"
        assert len(buttons) == 1 and window.get_focus() == buttons[0]
        snapshot(window, "update-copy-progress")
        grid, header = check_timeline(window, 2)
        progress_widgets.update(bar=bar, button=buttons[0], grid=grid, header=header,
                                header_y=header.get_allocation().y)
        window.connect("unmap", lambda *_: progress_unmaps.append(True))
        updates.status["progress"]["received"] = 10 * 1024**2
        updates.changed = True
    elif step == 8:
        bar, = [w for w in children_of(window) if isinstance(w, Gtk.ProgressBar)]
        assert bar is progress_widgets["bar"] and bar.get_fraction() == 0.5
        assert buttons[0] is progress_widgets["button"] and window.get_focus() == buttons[0]
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
        assert window.get_focus() == progress_widgets["button"]
        snapshot(window, "update-file-check")
        updates.status["operation"] = dict(name="save_system", elapsed=65)
        updates.changed = True
    elif step == 12:
        assert not progress_widgets["bar"].get_visible()
        assert "Current task: 1:05 elapsed" in titles
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
        check_timeline(window, 5, moving=False)
        snapshot(window, "update-complete")
        updates.status.update(phase="verifying", provider_launch_allowed=False,
                              operation=dict(name="unpack", received=45, total=100))
        updates.changed = True
    elif step == 15:
        check_timeline(window, 1)
        assert "Unpacking update files" in titles
        bar, = [w for w in children_of(window) if isinstance(w, Gtk.ProgressBar)]
        assert bar.get_text() == "45%"
        snapshot(window, "update-unpacking")
        updates.status.update(phase="ready_to_restart", can_restart=True, operation=None)
        updates.changed = True
    elif step == 16:
        check_timeline(window, 4, moving=False)
        snapshot(window, "update-ready")
        updates.status.update(phase="promoted", can_restart=False, provider_launch_allowed=True)
        updates.changed = True
    elif step == 17:
        for name in ("launcher", "compositor"):
            path = Path(os.environ["XDG_RUNTIME_DIR"]) / (name + "-heartbeat.json")
            assert path.is_file(), "Missing real Wayland/GTK heartbeat: " + name
        buttons[-1].clicked()
        assert updates.commands == ["install", "close"]
        done = True
        Gtk.main_quit()
        return False
    return True


def drive_updates(window, children, buttons, titles):
    global done
    if step == 0:
        assert "MAIN MENU" in titles and len(buttons) == 3
        snapshot(window, "main-menu-updates")
        assert "new version available" in buttons[2].get_label()
        assert window.get_focus() is buttons[0], "Updates stole initial gaming focus"
        assert buttons[2].get_valign() == Gtk.Align.START
        buttons[1].grab_focus()
        press(window, Gdk.KEY_Up)
        assert window.get_focus() is buttons[2]
        press(window, Gdk.KEY_Down)
        assert window.get_focus() is buttons[1], "Down did not return to the previous provider"
        buttons[2].clicked()
    elif step == 1:
        assert "SYSTEM UPDATES" in titles and len(buttons) == 3
        buttons[1].clicked()
        assert window.get_focus() != buttons[1]
        pads.actions = ["back"]
    elif step == 2:
        assert "SYSTEM UPDATES" in titles
        buttons[1].clicked()
        confirmation = [w for w in children_of(window) if isinstance(w, Gtk.Button)]
        assert window.get_focus() == confirmation[0]
        assert updates.commands == []
        confirmation[1].clicked()
    elif step == 3:
        assert "SYSTEM UPDATES" in titles and updates.commands == ["install"]
        buttons[1].clicked()
        confirmation = [w for w in children_of(window) if isinstance(w, Gtk.Button)]
        confirmation[0].clicked()
        assert updates.commands == ["install"]
    elif step == 4:
        updates.status["phase"] = "rolled_back"
        updates.changed = True
    elif step == 5:
        assert "SYSTEM UPDATES" in titles
        assert any("previous system was restored" in text for text in titles)
        buttons[1].clicked()
    elif step == 6:
        assert updates.commands == ["install", "dismiss"]
        buttons[-1].clicked()
    elif step == 7:
        assert "MAIN MENU" in titles and len(buttons) == 3
        buttons[0].clicked()
        control.pending = True
    elif step == 8:
        assert "GeForce NOW" in titles and len(buttons) == 3
        buttons[2].clicked()
    elif step == 9:
        assert "MAIN MENU" in titles and browser.service is None
        done = True
        Gtk.main_quit()
        return False
    return True


def drive():
    global step, done
    try:
        window, = [w for w in Gtk.Window.list_toplevels() if w.get_title() == "Cloudplay Home"]
        children = children_of(window)
        titles = [w.get_text() for w in children if isinstance(w, Gtk.Label)]
        title = next((text for text in titles if text in (
            "MAIN MENU", "GeForce NOW", "Xbox Cloud Gaming",
            "Streaming browser did not close")), "")
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
            assert title == "MAIN MENU" and len(buttons) == 2
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
            assert title == "Streaming browser did not close" and len(buttons) == 1
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
            done = True
            Gtk.main_quit()
            return False
        step += 1
        return True
    except BaseException as error:
        errors.append(error)
        Gtk.main_quit()
        return False


def timeout():
    errors.append(RuntimeError("Native UI smoke test timed out"))
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

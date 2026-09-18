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
from gi.repository import GLib, Gtk


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


updates = Updates() if update_mode else None


def drive_updates(window, children, buttons, titles):
    global done
    if step == 0:
        assert "MAIN MENU" in titles and len(buttons) == 3
        assert "new version available" in buttons[2].get_label()
        buttons[2].clicked()
    elif step == 1:
        assert "SYSTEM UPDATES" in titles and len(buttons) == 3
        buttons[1].clicked()
        assert window.get_focus() != buttons[1]
        pads.actions = ["back"]
    elif step == 2:
        assert "SYSTEM UPDATES" in titles
        buttons[1].clicked()
        confirmation = [w for w in window.get_child().get_children() if isinstance(w, Gtk.Button)]
        assert window.get_focus() == confirmation[0]
        assert updates.commands == []
        confirmation[1].clicked()
    elif step == 3:
        assert "SYSTEM UPDATES" in titles and updates.commands == ["install"]
        buttons[1].clicked()
        confirmation = [w for w in window.get_child().get_children() if isinstance(w, Gtk.Button)]
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
        children = window.get_child().get_children()
        titles = [w.get_text() for w in children if isinstance(w, Gtk.Label)]
        title = next((text for text in titles if text in (
            "MAIN MENU", "GeForce NOW", "Xbox Cloud Gaming",
            "Streaming browser did not close")), "")
        buttons = [w for w in children if isinstance(w, Gtk.Button)]
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
main.run(browser, control, pads, updates)
if errors:
    raise errors[0]
assert done and browser.service is None
if os.environ.get("CLOUDPLAY_SMOKE_RESULT"):
    Path(os.environ["CLOUDPLAY_SMOKE_RESULT"]).write_text('{"passed": true}\n')
print("Native Home smoke passed: GFN/Xbox navigation, confirmation, stay, reload, stop failure, crash recovery")

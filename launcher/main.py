#!/usr/bin/env python3
"""Native Wayland Home and host-owned leave confirmation; no web control API."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from host import Browser, Control, SERVICES, request_home
from gamepad import Gamepads


def main():
    import pwd
    if os.getuid() != 1000 or os.geteuid() != 1000 or pwd.getpwuid(1000).pw_name != "cloudplay":
        raise SystemExit("Run inside the cloudplay user session")
    runtime = Path(os.environ["XDG_RUNTIME_DIR"])
    if runtime.stat().st_uid != os.getuid() or not os.environ.get("WAYLAND_DISPLAY"):
        raise SystemExit("Missing owned Wayland runtime")
    os.umask(0o077)
    if sys.argv[1:] == ["home"]:
        try:
            request_home()
        except OSError:
            raise SystemExit("Cloudplay Home control is unavailable") from None
        return
    if sys.argv[1:]:
        raise SystemExit("Unsupported action")
    control = Control(runtime)
    try:
        browser = Browser(Path.home() / ".config/cloudplay")
    except BaseException:
        control.close()
        raise
    run(browser, control, Gamepads())


def run(browser, control, pads):
    os.environ["GDK_BACKEND"] = "wayland"
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk, GLib, Gtk

    window = Gtk.Window(title="Cloudplay Home")
    window.set_wmclass("cloudplay-home", "Cloudplay Home")
    window.fullscreen()
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
    box.set_halign(Gtk.Align.CENTER)
    box.set_valign(Gtk.Align.CENTER)
    box.set_border_width(48)
    window.add(box)
    css = Gtk.CssProvider()
    css.load_from_data(b"""
        window { background: #101b2b; color: #f1f5ff; }
        label { font-size: 22px; }
        button { font-size: 28px; padding: 22px 48px; border: 4px solid transparent; }
        button:focus { border-color: #66d6ef; background: #244b69; color: white; }
    """)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css,
                                             Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    buttons = []
    confirming = False
    recovering = False
    closing = False
    next_check = 0

    def show(title, choices, note):
        for child in box.get_children():
            box.remove(child)
            child.destroy()
        buttons.clear()
        box.pack_start(Gtk.Label(label=title), False, False, 0)
        for label, action in choices:
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _, action=action: action())
            box.pack_start(button, False, False, 0)
            buttons.append(button)
        label = Gtk.Label(label=note)
        label.set_line_wrap(True)
        label.set_max_width_chars(68)
        label.set_justify(Gtk.Justification.CENTER)
        box.pack_start(label, False, False, 0)
        # Remapping creates a newly focused native view, even over a fullscreen
        # service/error page. Never rely on JavaScript or focus inside Chromium.
        window.hide()
        window.show_all()
        window.fullscreen()
        window.present()
        buttons[0].grab_focus()

    def home(note=""):
        nonlocal confirming, recovering
        try:
            browser.stop()
        except (OSError, RuntimeError, subprocess.SubprocessError):
            confirming = True
            recovering = True
            show("Unable to close the streaming browser", [("Retry Return Home", home)],
                 "Home will not open until the browser has stopped. Login data is kept.")
            return
        confirming = False
        recovering = False
        show("Cloudplay Home",
             [(name, lambda service=service: launch(service))
              for service, (name, _, _) in SERVICES.items()],
             note or "D-pad + A to choose · Tab/arrows + Enter\n"
             "In a service: Ctrl+Alt+Home or hold Select/Back + Start/Menu for 2 seconds.")

    def launch(service):
        nonlocal confirming
        try:
            browser.start(service)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            home("Could not open the service. Try again; login data has been kept.")
            return
        confirming = False
        window.hide()

    def stay():
        nonlocal confirming
        confirming = False
        window.hide()

    def reload():
        service = browser.service
        if service:
            launch(service)
        else:
            home()

    def ask_home():
        nonlocal confirming
        if not browser.service:
            if not window.get_visible():
                home()
            return
        if confirming:
            return
        confirming = True
        show("Leave or reload " + SERVICES[browser.service][0] + "?",
             [("Stay in service", stay), ("Reload service", reload), ("Return Home", home)],
             "Returning Home closes the streaming browser. Reload restarts the service.\n"
             "Either may end your game; sign-in data is kept. B / Escape: stay.")

    def navigate(action):
        if action == "home":
            ask_home()
            return
        if not window.get_visible():
            return
        if action == "back" and confirming and not recovering:
            stay()
        elif action == "accept":
            focus = window.get_focus()
            if focus in buttons:
                focus.clicked()
        elif action in ("up", "left", "down", "right"):
            focus = window.get_focus()
            index = buttons.index(focus) if focus in buttons else 0
            buttons[(index + (-1 if action in ("up", "left") else 1)) % len(buttons)].grab_focus()

    def key(_, event):
        action = {Gdk.KEY_Escape: "back", Gdk.KEY_Up: "up", Gdk.KEY_Down: "down",
                  Gdk.KEY_Left: "left", Gdk.KEY_Right: "right"}.get(event.keyval)
        if action:
            navigate(action)
        return bool(action)

    def tick():
        nonlocal next_check
        if closing:
            Gtk.main_quit()
            return False
        if time.monotonic() >= next_check:
            next_check = time.monotonic() + 1
            try:
                if browser.exited():
                    home("The service browser closed. Choose a service to reopen it.")
            except (OSError, RuntimeError, subprocess.SubprocessError):
                home("Service supervision was interrupted. Choose a service to reopen it.")
        if control.poll():
            ask_home()
        for action in pads.poll(window.get_visible()):
            navigate(action)
        return True

    def stop(*_):
        nonlocal closing
        closing = True

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop)
    window.connect("delete-event", lambda *_: True)
    window.connect("key-press-event", key)
    try:
        home()
        GLib.timeout_add(50, tick)
        Gtk.main()
    finally:
        try:
            browser.stop()
        finally:
            pads.close()
            control.close()


if __name__ == "__main__":
    main()

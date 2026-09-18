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
from updates import ENABLED as OTA_ENABLED, Updates, actions as update_actions, badge as update_badge, summary as update_summary

LOGO = Path(__file__).with_name("assets") / "cloudplay-logo.png"
SERVICE_LOGOS = {
    "gfn": Path(__file__).with_name("assets") / "geforce-now-logo.png",
    "xbox": Path(__file__).with_name("assets") / "xbox-cloud-gaming-logo.png",
}
RETURN_ICONS = {
    "keyboard": Path(__file__).with_name("assets") / "keyboard-icon.png",
    "controller": Path(__file__).with_name("assets") / "controller-icon.png",
}


def action_labels(service):
    name = SERVICES[service][0]
    return ("Return to " + name, "Reload " + name, "Cloudplay OS Main Menu")


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
    run(browser, control, Gamepads(), Updates() if OTA_ENABLED.is_file() else None)


def run(browser, control, pads, updates=None, *, trusted_updates=False, heartbeats=None):
    os.environ["GDK_BACKEND"] = "wayland"
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

    window = Gtk.Window(title="Cloudplay Home")
    window.set_wmclass("cloudplay-home", "Cloudplay Home")
    window.fullscreen()
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
    box.set_halign(Gtk.Align.CENTER)
    box.set_valign(Gtk.Align.CENTER)
    box.set_border_width(56)
    window.add(box)
    css = Gtk.CssProvider()
    css.load_from_data(b"""
        window {
            background: #050910;
            color: #eef7ff;
        }
        image.brand-logo {
            margin-bottom: 4px;
        }
        label.page-title {
            color: #f5fbff;
            font-size: 31px;
            font-weight: bold;
            margin-top: 16px;
            margin-bottom: 8px;
        }
        label.status {
            color: #b9d3df;
            font-size: 18px;
            margin-bottom: 8px;
        }
        button {
            color: #eef7ff;
            background: #112235;
            border: 3px solid #29435a;
            border-radius: 16px;
            padding: 18px 28px;
        }
        button:hover {
            background: #183149;
        }
        button:focus {
            color: white;
            background: #1d4058;
            border-color: #66dfe6;
            box-shadow: 0 0 0 3px rgba(102, 223, 230, 0.25);
        }
        button.service-card {
            min-width: 720px;
            min-height: 88px;
            padding: 14px 24px;
        }
        button.gfn-card {
            border-left: 8px solid #76b900;
        }
        button.xbox-card {
            border-left: 8px solid #107c10;
        }
        box.service-logo-frame {
            min-width: 190px;
            min-height: 60px;
        }
        image.service-logo {
            margin: 2px 0;
        }
        label.service-name {
            color: white;
            font-size: 27px;
            font-weight: bold;
        }
        label.service-detail {
            color: #9eb8c8;
            font-size: 16px;
        }
        label.chevron {
            color: #66dfe6;
            font-size: 34px;
            font-weight: bold;
        }
        button.action {
            min-width: 620px;
            min-height: 48px;
            font-size: 23px;
        }
        button.main-menu {
            border-color: #3b7582;
        }
        box.return-help {
            margin-top: 18px;
            padding: 7px;
        }
        image.return-icon {
            min-width: 80px;
            min-height: 50px;
        }
        label.return-text {
            color: #adc4d1;
            font-size: 17px;
        }
    """)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css,
                                             Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    buttons = []
    confirming = False
    recovering = False
    closing = False
    next_check = 0
    updates_screen = False
    update_confirmation = False
    update_notice = None

    def style(widget, *names):
        context = widget.get_style_context()
        for name in names:
            context.add_class(name)
        return widget

    def add_brand():
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            str(LOGO), 520, 220, True)
        box.pack_start(style(Gtk.Image.new_from_pixbuf(pixbuf), "brand-logo"),
                       False, False, 0)

    def service_button(service, action):
        name = SERVICES[service][0]
        button = style(Gtk.Button(), "service-card", service + "-card")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        logo_frame = style(Gtk.Box(), "service-logo-frame")
        logo_frame.set_size_request(190, 60)
        logo_frame.set_halign(Gtk.Align.CENTER)
        logo_frame.set_valign(Gtk.Align.CENTER)
        logo = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            str(SERVICE_LOGOS[service]), 180, 56, True)
        logo_frame.pack_start(style(Gtk.Image.new_from_pixbuf(logo), "service-logo"),
                              False, False, 0)
        row.pack_start(logo_frame, False, False, 0)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        labels.set_valign(Gtk.Align.CENTER)
        name_label = style(Gtk.Label(label=name), "service-name")
        name_label.set_xalign(0)
        detail = style(Gtk.Label(label="NVIDIA cloud gaming" if service == "gfn"
                                 else "Xbox games from the cloud"), "service-detail")
        detail.set_xalign(0)
        labels.pack_start(name_label, False, False, 0)
        labels.pack_start(detail, False, False, 0)
        row.pack_start(labels, True, True, 0)
        row.pack_end(style(Gtk.Label(label=">"), "chevron"), False, False, 0)
        button.add(row)
        button.connect("clicked", lambda _: action())
        return button

    def action_button(label, action, icon=None, main_menu=False):
        button = style(Gtk.Button(), "action")
        if main_menu:
            style(button, "main-menu")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        if icon:
            row.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.LARGE_TOOLBAR),
                           False, False, 0)
        text = Gtk.Label(label=label)
        text.set_xalign(0)
        row.pack_start(text, True, True, 0)
        button.add(row)
        button.connect("clicked", lambda _: action())
        return button

    def add_return_help():
        for icon, text in (
                ("keyboard",
                 "Press Ctrl + Alt + Home to return to this menu"),
                ("controller",
                 "Hold Select/Back + Start/Menu for two seconds to return to this menu")):
            row = style(Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14),
                        "return-help")
            row.set_halign(Gtk.Align.CENTER)
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(RETURN_ICONS[icon]), 80, 50, True)
            row.pack_start(style(Gtk.Image.new_from_pixbuf(pixbuf), "return-icon"),
                           False, False, 0)
            row.pack_start(style(Gtk.Label(label=text), "return-text"), False, False, 0)
            box.pack_start(row, False, False, 0)

    def show(title, choices, note="", services=False, return_help=False):
        nonlocal updates_screen, update_confirmation, update_notice
        updates_screen = False
        update_confirmation = title == "CONFIRM UPDATE"
        update_notice = None
        for child in box.get_children():
            box.remove(child)
            child.destroy()
        buttons.clear()
        add_brand()
        box.pack_start(style(Gtk.Label(label=title), "page-title"), False, False, 0)
        if note:
            status = style(Gtk.Label(label=note), "status")
            status.set_line_wrap(True)
            status.set_max_width_chars(72)
            status.set_justify(Gtk.Justification.CENTER)
            box.pack_start(status, False, False, 0)
        for choice in choices:
            if services:
                service, action = choice
                button = service_button(service, action)
            else:
                label, action, icon, main_menu = choice
                button = action_button(label, action, icon, main_menu)
            box.pack_start(button, False, False, 0)
            buttons.append(button)
        if return_help:
            add_return_help()
        if services and updates is not None:
            update_notice = Gtk.Button(label=update_badge(updates.status))
            update_notice.set_halign(Gtk.Align.END)
            update_notice.connect("clicked", lambda _: show_updates())
            box.pack_start(update_notice, False, False, 0)
            buttons.append(update_notice)
        # Remapping creates a newly focused native view, even over a fullscreen
        # service/error page. Never rely on JavaScript or focus inside Chromium.
        window.hide()
        window.show_all()
        window.fullscreen()
        window.present()
        buttons[0].grab_focus()

    def show_updates(note=""):
        nonlocal updates_screen
        if updates is None or browser.service:
            return
        choices = [(label, lambda command=command: update_action(command),
                    "software-update-available-symbolic", False)
                   for label, command in update_actions(updates.status)]
        if trusted_updates:
            if updates.status.get("provider_launch_allowed") is True:
                choices.append(("Return to Main Menu", lambda: submit_update("close"),
                                "go-home-symbolic", True))
            if not choices:
                choices.append(("Refresh Status", lambda: submit_update("status"),
                                "view-refresh-symbolic", False))
        else:
            choices.append(("Cloudplay OS Main Menu", home, "go-home-symbolic", True))
        message = note or updates.error or update_summary(updates.status)
        show("SYSTEM UPDATES", choices, message)
        updates_screen = True

    def update_action(command):
        if updates is None or browser.service:
            return
        if command in ("install", "restart"):
            label = "Install Update" if command == "install" else "Restart to Update"
            note = ("The inactive system slot will be replaced. Keep the power connected."
                    if command == "install" else
                    "Cloudplay OS will restart now and check the updated system.")
            show("CONFIRM UPDATE",
                 [("Not Now", show_updates, "go-previous-symbolic", False),
                  (label, lambda: submit_update(command), "system-reboot-symbolic", False)], note)
        else:
            submit_update(command)

    def submit_update(command):
        if updates.submit(command):
            show_updates("Sending update request...")
        else:
            show_updates("An update request is already in progress.")

    def home(note=""):
        nonlocal confirming, recovering
        if trusted_updates:
            show_updates(note)
            return
        try:
            browser.stop()
        except (OSError, RuntimeError, subprocess.SubprocessError):
            confirming = True
            recovering = True
            show("Streaming browser did not close",
                 [("Try Again", home, "view-refresh-symbolic", False)],
                 "Cloudplay OS must close it safely before returning to the Main Menu.")
            return
        confirming = False
        recovering = False
        show("MAIN MENU",
             [(service, lambda service=service: launch(service)) for service in SERVICES],
             note, services=True, return_help=True)

    def launch(service):
        nonlocal confirming
        if updates is not None and (
                updates.error or updates.status.get("provider_launch_allowed") is not True):
            home("The update service has not cleared this session to launch. Check System Updates.")
            return
        try:
            browser.start(service)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            home(SERVICES[service][0] + " could not be opened. Your sign-in is still saved.")
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
            if not window.get_visible() or updates_screen or update_confirmation:
                home()
            return
        if confirming:
            return
        confirming = True
        return_label, reload_label, home_label = action_labels(browser.service)
        show(SERVICES[browser.service][0],
             [(return_label, stay, "go-previous-symbolic", False),
              (reload_label, reload, "view-refresh-symbolic", False),
              (home_label, home, "go-home-symbolic", True)])

    def navigate(action):
        if action == "home":
            ask_home()
            return
        if not window.get_visible():
            return
        if action == "back" and confirming and not recovering:
            stay()
        elif action == "back" and update_confirmation:
            show_updates()
        elif action == "back" and updates_screen:
            home()
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
        if heartbeats is not None:
            heartbeats.tick()
        if time.monotonic() >= next_check:
            next_check = time.monotonic() + 1
            try:
                if browser.exited():
                    home("The streaming service closed. Choose where to play.")
            except (OSError, RuntimeError, subprocess.SubprocessError):
                home("The streaming session ended unexpectedly. Choose where to play.")
        if control.poll():
            ask_home()
        if updates is not None and updates.poll():
            if updates_screen and not browser.service:
                old_index = buttons.index(window.get_focus()) if window.get_focus() in buttons else 0
                show_updates()
                buttons[min(old_index, len(buttons) - 1)].grab_focus()
            elif update_notice is not None:
                update_notice.set_label(update_badge(updates.status))
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
            if updates is not None:
                updates.close()
            if heartbeats is not None:
                heartbeats.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Native Wayland Home and host-owned leave confirmation; no web control API."""
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from host import Browser, Control, SERVICES, request_home
from gamepad import Gamepads
from updates import ENABLED as OTA_ENABLED, Updates, actions as update_actions, badge as update_badge, summary as update_summary
from updates import BUSY as UPDATE_BUSY, progress_fraction, progress_text, journey, version_text, request_text, error_detail
from reporting import collect_report, qr_pixels

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


def run(browser, control, pads, updates=None, *, trusted_updates=False, heartbeats=None,
        start_page="updates"):
    os.environ["GDK_BACKEND"] = "wayland"
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

    display = Gdk.Display.get_default()
    monitor = display.get_primary_monitor() or display.get_monitor(0)
    geometry = monitor.get_geometry()
    scale = min(1.0, geometry.width / 1920, geometry.height / 1080)
    def pixels(value):
        return max(1, round(value * scale))

    window = Gtk.Window(title="Cloudplay Home")
    window.set_wmclass("cloudplay-home", "Cloudplay Home")
    window.fullscreen()
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=pixels(18))
    box.set_halign(Gtk.Align.CENTER)
    box.set_valign(Gtk.Align.CENTER)
    box.set_border_width(pixels(56))
    overlay = Gtk.Overlay()
    overlay.add(box)
    window.add(overlay)
    css = Gtk.CssProvider()
    stylesheet = """
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
            font-size: 22px;
            margin-bottom: 8px;
        }
        progressbar {
            color: #b9d3df;
            font-size: 22px;
        }
        label.update-version {
            color: #91adbf;
            font-size: 20px;
            margin-bottom: 18px;
        }
        label.stage-name {
            color: #8196a8;
            font-size: 20px;
        }
        label.stage-name.active { color: #eef7ff; font-weight: bold; }
        label.stage-name.done { color: #66dfe6; }
        frame.stage-marker {
            border: 2px solid #3a5063;
            border-radius: 24px;
            background: #112235;
            color: #8196a8;
            font-size: 24px;
        }
        frame.stage-marker.active {
            border-color: #66dfe6;
            color: #66dfe6;
            box-shadow: 0 0 0 4px rgba(102, 223, 230, 0.12);
        }
        frame.stage-marker.done {
            border-color: #66dfe6;
            background: #1d4058;
            color: #66dfe6;
        }
        separator.stage-link { min-height: 3px; background: #29435a; }
        separator.stage-link.done { background: #66dfe6; }
        progressbar trough {
            min-height: 24px;
            background: #112235;
            border: 1px solid #29435a;
            border-radius: 13px;
        }
        progressbar progress {
            min-height: 24px;
            background: linear-gradient(to bottom, #83edf0, #35b7c9);
            border-radius: 12px;
            border: none;
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
        button.updates-shortcut {
            font-size: 22px;
            padding: 10px 18px;
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
    """
    css.load_from_data(re.sub(r"(\d+)px", lambda match: str(pixels(int(match[1]))) + "px",
                             stylesheet).encode("ascii"))
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css,
                                             Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    buttons = []
    confirming = False
    recovering = False
    closing = False
    next_check = 0
    updates_screen = False
    settings_screen = False
    report_screen = False
    settings_channel = None
    beta_screen = False
    beta_choices = None
    update_confirmation = False
    update_notice = None
    settings_shortcut = None
    update_message = None
    update_progress = None
    update_choices = None
    update_journey = None
    update_version = None
    update_activity_text = None
    post_update_seen = False
    auto_return_attempted = False
    check_updates_on_entry = False
    home_focus = 0

    def style(widget, *names):
        context = widget.get_style_context()
        for name in names:
            context.add_class(name)
        return widget

    def add_brand(compact=False):
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            str(LOGO), pixels(380 if compact else 520), pixels(160 if compact else 220), True)
        box.pack_start(style(Gtk.Image.new_from_pixbuf(pixbuf), "brand-logo"),
                       False, False, 0)

    def service_button(service, action):
        name = SERVICES[service][0]
        button = style(Gtk.Button(), "service-card", service + "-card")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=pixels(24))
        logo_frame = style(Gtk.Box(), "service-logo-frame")
        logo_frame.set_size_request(pixels(190), pixels(60))
        logo_frame.set_halign(Gtk.Align.CENTER)
        logo_frame.set_valign(Gtk.Align.CENTER)
        logo = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            str(SERVICE_LOGOS[service]), pixels(180), pixels(56), True)
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
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=pixels(16))
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
            row = style(Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=pixels(14)),
                        "return-help")
            row.set_halign(Gtk.Align.CENTER)
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(RETURN_ICONS[icon]), pixels(80), pixels(50), True)
            row.pack_start(style(Gtk.Image.new_from_pixbuf(pixbuf), "return-icon"),
                           False, False, 0)
            row.pack_start(style(Gtk.Label(label=text), "return-text"), False, False, 0)
            box.pack_start(row, False, False, 0)

    def add_timeline(stages):
        grid = Gtk.Grid(column_spacing=pixels(12), row_spacing=pixels(14))
        grid.set_halign(Gtk.Align.CENTER)
        grid.set_margin_top(pixels(8))
        grid.set_margin_bottom(pixels(20))
        items = []
        for position, (name, _) in enumerate(stages):
            marker = style(Gtk.Frame(), "stage-marker")
            marker.set_shadow_type(Gtk.ShadowType.NONE)
            marker.set_halign(Gtk.Align.CENTER)
            marker.set_size_request(pixels(44), pixels(44))
            content = Gtk.Overlay()
            marker.add(content)
            symbol = Gtk.Label()
            content.add(symbol)
            spinner = Gtk.Spinner()
            spinner.set_halign(Gtk.Align.CENTER)
            spinner.set_valign(Gtk.Align.CENTER)
            spinner.set_size_request(pixels(24), pixels(24))
            spinner.set_no_show_all(True)
            content.add_overlay(spinner)
            label = style(Gtk.Label(label=name), "stage-name")
            label.set_size_request(pixels(125), -1)
            grid.attach(marker, position * 2, 0, 1, 1)
            grid.attach(label, position * 2, 1, 1, 1)
            link = None
            if position < len(stages) - 1:
                link = style(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), "stage-link")
                link.set_size_request(pixels(36), -1)
                link.set_valign(Gtk.Align.CENTER)
                grid.attach(link, position * 2 + 1, 0, 1, 1)
            items.append((marker, symbol, spinner, label, link))
        box.pack_start(grid, False, False, 0)
        return items

    def refresh_timeline(stages, animate):
        for (name, state), (marker, symbol, spinner, label, link) in zip(stages, update_journey):
            for widget in (marker, label):
                context = widget.get_style_context()
                for old_state in ("active", "done", "upcoming"):
                    context.remove_class(old_state)
                context.add_class(state)
            moving = state == "active" and animate
            symbol.set_text("\u2713" if state == "done" else "" if moving else "\u2022")
            spinner.set_visible(moving)
            if moving:
                spinner.start()
            else:
                spinner.stop()
            marker.get_accessible().set_name(f"{name}: {state}")
            if link is not None:
                context = link.get_style_context()
                if state == "done":
                    context.add_class("done")
                else:
                    context.remove_class("done")

    def show(title, choices, note="", services=False, return_help=False, activity=False,
             stages=(), checking=False):
        nonlocal updates_screen, settings_screen, beta_screen, beta_choices, report_screen
        nonlocal update_confirmation, update_notice, settings_shortcut
        nonlocal update_message, update_progress, update_choices
        nonlocal update_journey, update_activity_text, update_version
        updates_screen = False
        settings_screen = title == "SETTINGS"
        report_screen = title == "REPORT A PROBLEM"
        beta_screen = title == "BETA RELEASES"
        beta_choices = None
        update_confirmation = title == "CONFIRM UPDATE"
        for shortcut in (update_notice, settings_shortcut):
            if shortcut is not None:
                overlay.remove(shortcut)
                shortcut.destroy()
        update_notice = settings_shortcut = None
        update_message = update_progress = update_choices = None
        update_journey = update_activity_text = update_version = None
        for child in box.get_children():
            box.remove(child)
            child.destroy()
        buttons.clear()
        update_page = title in ("SYSTEM UPDATES", "CONFIRM UPDATE")
        box.set_valign(Gtk.Align.START if update_page else Gtk.Align.CENTER)
        add_brand(compact=update_page or report_screen)
        box.pack_start(style(Gtk.Label(label=title), "page-title"), False, False, 0)
        if update_page:
            update_version = style(Gtk.Label(label=version_text(updates.status if updates else {})), "update-version")
            update_version.set_line_wrap(True)
            update_version.set_max_width_chars(68)
            update_version.set_justify(Gtk.Justification.CENTER)
            box.pack_start(update_version, False, False, 0)
        if stages:
            update_journey = add_timeline(stages)
        if note:
            status = style(Gtk.Label(label=note), "status")
            status.set_line_wrap(True)
            status.set_max_width_chars(68)
            status.set_justify(Gtk.Justification.CENTER)
            box.pack_start(status, False, False, 0)
            update_message = status
        if checking:
            spinner = Gtk.Spinner()
            spinner.set_size_request(pixels(32), pixels(32))
            spinner.set_halign(Gtk.Align.CENTER)
            spinner.start()
            box.pack_start(spinner, False, False, 0)
        if activity:
            update_activity_text = style(Gtk.Label(), "status")
            box.pack_start(update_activity_text, False, False, 0)
            update_progress = Gtk.ProgressBar()
            update_progress.set_show_text(True)
            update_progress.set_no_show_all(True)
            box.pack_start(update_progress, False, False, 0)
            box.pack_start(style(Gtk.Label(label="Do not disconnect from power."), "status"),
                           False, False, 0)
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
        if services:
            settings_shortcut = style(Gtk.Button(label="Settings"), "updates-shortcut")
            settings_shortcut.set_halign(Gtk.Align.START)
            settings_shortcut.set_valign(Gtk.Align.START)
            settings_shortcut.set_margin_top(pixels(40))
            settings_shortcut.set_margin_start(pixels(56))
            settings_shortcut.connect("clicked", lambda _: show_settings())
            overlay.add_overlay(settings_shortcut)
            buttons.append(settings_shortcut)
        if services and updates is not None:
            update_notice = style(Gtk.Button(label=update_badge(updates.status)), "updates-shortcut")
            update_notice.set_no_show_all(True)
            bell = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(LOGO.with_name("updates-bell.svg")), pixels(28), pixels(28), True)
            update_notice.set_image(Gtk.Image.new_from_pixbuf(bell))
            update_notice.set_always_show_image(True)
            update_notice.set_halign(Gtk.Align.END)
            update_notice.set_valign(Gtk.Align.START)
            update_notice.set_margin_top(pixels(40))
            update_notice.set_margin_end(pixels(56))
            update_notice.connect("clicked", lambda _: open_updates())
            overlay.add_overlay(update_notice)
            refresh_update_notice()
        # Remapping creates a newly focused native view, even over a fullscreen
        # service/error page. Never rely on JavaScript or focus inside Chromium.
        window.hide()
        window.show_all()
        window.fullscreen()
        window.present()
        if buttons:
            buttons[home_focus if services else 0].grab_focus()
        else:
            window.set_focus(None)

    def refresh_update_notice():
        label = update_badge(updates.status)
        visible = label != "Updates"
        update_notice.set_label(label)
        if visible and update_notice not in buttons:
            buttons.append(update_notice)
        elif not visible and update_notice in buttons:
            if window.get_focus() is update_notice:
                settings_shortcut.grab_focus()
            buttons.remove(update_notice)
        update_notice.set_visible(visible)

    def show_settings():
        nonlocal settings_channel
        if browser.service:
            return
        if trusted_updates and updates.status.get("provider_launch_allowed") is not True:
            show_updates()
            return
        channel = updates.status.get("channel") if updates else None
        settings_channel = channel
        beta_label = "Beta Releases" + (" - On" if channel == "beta" else
                                       " - Off" if channel == "stable" else "")
        show("SETTINGS",
             [("System Updates", open_updates, "software-update-available-symbolic", False),
              (beta_label, open_beta, "preferences-system-symbolic", False),
              ("Report a problem", show_report, "dialog-information-symbolic", False),
              ("Main Menu", lambda: submit_update("close") if trusted_updates else home(),
               "go-home-symbolic", True)])

    def show_report():
        if browser.service:
            return
        if trusted_updates and updates.status.get("provider_launch_allowed") is not True:
            show_updates()
            return
        choices = [("Back to Settings", show_settings, "go-previous-symbolic", True)]
        try:
            summary, url = collect_report(updates.status if updates else {"phase": "unavailable"})
            width, rgb = qr_pixels(url, max(4, round(6 * scale)))
            pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
                GLib.Bytes.new(rgb), GdkPixbuf.Colorspace.RGB, False, 8, width, width, width * 3)
        except (OSError, ValueError, ImportError):
            logging.getLogger(__name__).exception("Problem report QR could not be prepared")
            show("REPORT A PROBLEM", choices,
                 "Unable to prepare the report code.\n"
                 "On your phone, visit github.com/sslivins/cloudplay-os/issues\n"
                 "GitHub sign-in is required. Submitted issues are public.")
            return
        show("REPORT A PROBLEM", choices)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=pixels(36))
        image = Gtk.Image.new_from_pixbuf(pixbuf)
        image.get_accessible().set_name("Scan to open a GitHub issue draft")
        row.pack_start(image, False, False, 0)
        text = style(Gtk.Label(label=(
            "Scan with your phone to describe the problem.\n"
            "GitHub sign-in is required. Submitted issues are public.\n"
            "Nothing is posted until you submit on your phone.\n\n"
            "Only this diagnostic summary is prefilled:\n" + summary +
            "\n\nNo logs, account details or network identifiers.\n"
            "Please do not add passwords or other private information.")), "status")
        text.set_line_wrap(True)
        text.set_max_width_chars(44)
        text.set_xalign(0)
        row.pack_start(text, False, False, 0)
        box.pack_start(row, False, False, 0)
        box.reorder_child(row, 2)
        row.show_all()

    def open_beta():
        if browser.service:
            return
        if updates is not None and not trusted_updates:
            accepted = updates.submit("open-beta")
            show_beta(request_text("open-beta") if accepted else "")
        else:
            show_beta()

    def submit_beta(command):
        if updates.submit(command):
            show_beta(request_text(command))

    def show_beta(note=""):
        nonlocal beta_choices
        if browser.service:
            return
        channel = updates.status.get("channel") if updates else None
        description = ("Try new features before they're available to everyone.\n"
                       "Beta releases may have bugs.\n"
                       "Turning this off keeps your current version.\n"
                       "You'll get regular updates when a newer version is available.")
        state = "On" if channel == "beta" else "Off" if channel == "stable" else "Checking..."
        message = f"Beta releases: {state}\n\n{description}"
        choices = []
        if updates is None:
            message = "Beta releases aren't available on this installation."
        elif trusted_updates:
            if updates.status.get("channel_change_enabled") is True and channel in ("stable", "beta"):
                command = "disable_beta" if channel == "beta" else "enable_beta"
                choices.append(("Turn Off Beta Releases" if channel == "beta" else "Turn On Beta Releases",
                                lambda: submit_beta(command), "preferences-system-symbolic", False))
            elif channel in ("stable", "beta"):
                message += "\nFinish the current update before changing this preference."
        elif updates.error:
            choices.append(("Try Again", open_beta, "view-refresh-symbolic", False))
        choices.append(("Back to Settings", show_settings, "go-previous-symbolic", True))
        error = (updates.error or updates.status.get("error")) if updates else None
        if error:
            message += "\n" + (error if isinstance(error, str) else error_detail(error))
        if note:
            message += "\n" + note
        key = tuple(choice[0] for choice in choices)
        if not beta_screen or beta_choices != key:
            show("BETA RELEASES", choices, message)
            beta_choices = key
        else:
            update_message.set_text(message)

    def open_updates():
        nonlocal check_updates_on_entry
        if browser.service:
            return
        if updates is not None and not trusted_updates:
            submit_update("open")
        else:
            check_updates_on_entry = True
            show_updates()

    def show_updates(note=""):
        nonlocal updates_screen, update_choices, post_update_seen, auto_return_attempted
        nonlocal check_updates_on_entry
        if browser.service:
            return
        if updates is None:
            show("SYSTEM UPDATES",
                 [("Back to Settings", show_settings, "go-previous-symbolic", True)],
                 "Updates aren't available on this installation.")
            updates_screen = True
            return
        status = updates.display_status
        if check_updates_on_entry and status.get("phase", "unknown") != "unknown":
            check_updates_on_entry = False
            if (status.get("phase") in ("idle", "available", "promoted", "failed", "rolled_back")
                    and status.get("provider_launch_allowed") is True):
                submit_update("check")
                return
        post_update = trusted_updates and status.get("phase") in ("tryboot_running", "promoting", "returning")
        if trusted_updates and status.get("phase") in ("tryboot_running", "promoting"):
            post_update_seen = True
        if (trusted_updates and post_update_seen and not auto_return_attempted
                and status.get("phase") == "promoted"
                and status.get("provider_launch_allowed") is True and not updates.error):
            auto_return_attempted = True
            submit_update("close")
            return
        choices = [(label, lambda command=command: update_action(command),
                    "software-update-available-symbolic", False)
                   for label, command in update_actions(status)]
        if not trusted_updates:
            choices = ([("Try Again", open_updates, "view-refresh-symbolic", False)]
                       if updates.error else [])
        if trusted_updates:
            if status.get("provider_launch_allowed") is True:
                choices.append(("Back to Settings", show_settings, "go-previous-symbolic", True))
        else:
            choices.append(("Back to Settings", show_settings, "go-previous-symbolic", True))
        message = note or updates.error or update_summary(status, include_progress=False)
        checking = status.get("phase") == "checking" and not updates.error
        activity = status.get("phase") in UPDATE_BUSY and not updates.error and not checking
        stages = () if post_update else journey(status)
        choice_key = (tuple(choice[0] for choice in choices), activity, bool(stages), post_update, checking)
        if not updates_screen or choice_key != update_choices:
            focus = window.get_focus()
            old_action = (update_choices[0][buttons.index(focus)]
                          if updates_screen and update_choices and focus in buttons else None)
            show("SYSTEM UPDATES", choices, message, activity=activity, stages=stages, checking=checking)
            updates_screen = True
            update_choices = choice_key
            if old_action in choice_key[0]:
                buttons[choice_key[0].index(old_action)].grab_focus()
        else:
            update_message.set_text(message)
        update_version.set_text(version_text(status))
        update_version.set_visible(not post_update)
        if update_journey is not None:
            refresh_timeline(stages, activity and not status.get("error"))
        if update_progress is not None:
            fraction = progress_fraction(status)
            update_progress.set_visible(fraction is not None)
            if fraction is not None:
                update_progress.set_fraction(fraction)
            update_progress.set_text(progress_text(status))
            update_activity_text.set_text("" if fraction is not None else progress_text(status))

    def update_action(command):
        if updates is None or browser.service:
            return
        if command == "install":
            note = ("You won't be able to play while the update installs.\n"
                    "This may take several minutes. Do not disconnect from power.\n"
                    "Your Cloudplay device will restart automatically when the update is complete.")
            show("CONFIRM UPDATE",
                 [("Not Now", show_updates, "go-previous-symbolic", False),
                  ("Install Update", lambda: submit_update(command), "system-reboot-symbolic", False)], note)
        else:
            submit_update(command)

    def submit_update(command):
        if updates.submit(command):
            show_updates(request_text(command))
        else:
            show_updates()

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
            show("Could not return to the Main Menu",
                 [("Try Again", home, "view-refresh-symbolic", False)],
                 "Please try again to close the game and return to the Main Menu.")
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
            home("Please open System Updates before starting a game.")
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
            if not window.get_visible() or updates_screen or update_confirmation or settings_screen or beta_screen or report_screen:
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
        nonlocal home_focus
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
            if trusted_updates:
                if updates.display_status.get("provider_launch_allowed") is True:
                    show_settings()
            else:
                show_settings()
        elif action == "back" and beta_screen:
            show_settings()
        elif action == "back" and report_screen:
            show_settings()
        elif action == "back" and settings_screen:
            if trusted_updates:
                submit_update("close")
            else:
                home()
        elif action == "accept":
            focus = window.get_focus()
            if focus in buttons:
                focus.clicked()
        elif action in ("up", "left", "down", "right") and buttons:
            focus = window.get_focus()
            index = buttons.index(focus) if focus in buttons else 0
            if settings_shortcut is not None:
                shortcuts = [settings_shortcut]
                if update_notice is not None and update_notice.get_visible():
                    shortcuts.append(update_notice)
                if action == "up" and focus not in shortcuts:
                    home_focus = min(index, len(SERVICES) - 1)
                    shortcuts[-1].grab_focus()
                    return
                if focus in shortcuts:
                    if action in ("up", "down"):
                        buttons[home_focus].grab_focus()
                    else:
                        shortcuts[(shortcuts.index(focus) + (-1 if action == "left" else 1))
                                  % len(shortcuts)].grab_focus()
                    return
            buttons[(index + (-1 if action in ("up", "left") else 1)) % len(buttons)].grab_focus()

    def key(_, event):
        action = {Gdk.KEY_Escape: "back", Gdk.KEY_Up: "up", Gdk.KEY_Down: "down",
                  Gdk.KEY_Left: "left", Gdk.KEY_Right: "right",
                  Gdk.KEY_Return: "accept", Gdk.KEY_KP_Enter: "accept",
                  Gdk.KEY_space: "accept"}.get(event.keyval)
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
                    home("Your game closed. Choose where to play.")
            except (OSError, RuntimeError, subprocess.SubprocessError):
                home("Your game closed unexpectedly. Choose where to play.")
        if control.poll():
            ask_home()
        if updates is not None and updates.poll():
            if updates_screen and not browser.service:
                show_updates()
            elif beta_screen and not browser.service:
                show_beta()
            elif settings_screen and not browser.service:
                if updates.status.get("channel") != settings_channel:
                    focus = window.get_focus()
                    index = buttons.index(focus) if focus in buttons else 0
                    show_settings()
                    buttons[min(index, len(buttons) - 1)].grab_focus()
            elif update_notice is not None:
                refresh_update_notice()
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
        if trusted_updates and start_page == "beta":
            show_beta()
        elif trusted_updates:
            open_updates()
        else:
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

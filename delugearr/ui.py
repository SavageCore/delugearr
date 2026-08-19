"""Delugearr UI pages (NiceGUI): login, dashboard, history, settings."""

import logging
import threading
import time
from datetime import datetime

from nicegui import app, events, run, ui

from . import config
from .deluge_client import DelugeClient
from .notifier import DEFAULT_AVATAR, fmt_ratio, fmt_seeding, make_notifier

log = logging.getLogger("delugearr-ui")

TRIGGER_LABELS = {
    "scan_summary": "Scan summary",
    "removals": "Per-torrent removals",
    "errors": "Errors",
    "manual_actions": "Manual actions",
}

ACTION_LABELS = {
    "would_remove_data": "Would remove + data",
    "would_remove_only": "Would remove (keep data)",
    "removed_data": "Removed + data",
    "removed_only": "Removed (kept data)",
    "manual_removed_data": "Manually removed + data",
    "manual_removed_only": "Manually removed (kept data)",
    "error": "Failed",
}

VALUE_TO_THEME = {None: "system", True: "light", False: "dark"}


def message_category(message):
    return (message or "").split(":")[0].strip()


def fmt_size(value):
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return ""
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{size:.1f} {units[index]}" if index else f"{int(size)} B"


def fmt_ts(value):
    if not value:
        return ""
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""


def setup_theme():
    dark = ui.dark_mode()
    theme = app.storage.user.get("theme")
    dark.set_value(theme if theme in (True, False) else None)
    return dark


def theme_icon(value):
    return {"system": "brightness_auto", "light": "light_mode", "dark": "dark_mode"}[
        VALUE_TO_THEME.get(value, "system")
    ]


def theme_label(value):
    return {"system": "Theme: system", "light": "Theme: light", "dark": "Theme: dark"}[
        VALUE_TO_THEME.get(value, "system")
    ]


def theme_cycle_button(dark):
    order = [None, True, False]
    button = ui.button(icon=theme_icon(dark.value)).props("flat round dense").classes("text-white")
    button.tooltip(theme_label(dark.value))

    def cycle():
        index = order.index(dark.value) if dark.value in order else 0
        dark.set_value(order[(index + 1) % 3])
        app.storage.user["theme"] = dark.value
        button.props(f"icon={theme_icon(dark.value)}")
        button.tooltip(theme_label(dark.value))

    button.on("click", cycle)
    return button


def banner(text, color):
    ui.label(text).classes(f"w-full text-center font-bold py-2 px-4 {color}")


def header(current):
    dark = setup_theme()
    with (
        ui.header(elevated=True).classes("items-center px-4 py-2"),
        ui.row().classes("items-center justify-between w-full gap-4"),
    ):
        with ui.row().classes("items-center gap-2"):
            ui.image(DEFAULT_AVATAR).classes("w-8 h-8 rounded")
            ui.label("Delugearr").classes("text-xl font-bold")
        with ui.row().classes("items-center gap-1"):
            for name, path in (("Dashboard", "/"), ("History", "/history"), ("Settings", "/settings")):
                active = "bg-white/15" if name == current else "hover:bg-white/10"
                ui.link(name, path).classes(
                    f"px-3 py-1.5 rounded-lg font-semibold no-underline text-white {active}"
                )
        with ui.row().classes("items-center gap-2"):
            theme_cycle_button(dark)
            ui.button(icon="logout").props("flat round dense").classes("text-white").on(
                "click", logout
            ).tooltip("Log out")


def logout():
    app.storage.user.clear()
    ui.navigate.to("/login")


def confirm_dialog(title, message, on_confirm):
    dialog = ui.dialog()
    with dialog, ui.card():
        ui.label(title).classes("text-lg font-bold")
        ui.label(message)
        with ui.row().classes("gap-2 pt-2"):
            ui.button("Cancel", on_click=dialog.close)
            ui.button("Confirm", on_click=lambda: (on_confirm(), dialog.close())).props("color=negative")
    dialog.open()


def detections_rows(store):
    """Latest scan's unregistered torrents, minus ones already handled by hand.

    Manually-removed torrents are gone from Deluge (their audit rows live in
    history), and exempted ones are deliberately left alone, so neither should
    clutter the dashboard's actionable list.
    """
    rows = []
    for det in store.current_detections():
        rows.append(
            {
                "hash": det["torrent_hash"],
                "name": det["name"],
                "label": det["label"],
                "tracker": det["tracker"],
                "message": det["message"],
                "size": fmt_size(det["size"]),
                "action": ACTION_LABELS.get(det["action"], det["action"]),
                "ts": fmt_ts(det["ts"]),
            }
        )
    return rows


def exempt_rows(store):
    return [
        {"hash": r["torrent_hash"], "reason": r["reason"], "ts": fmt_ts(r["added_ts"])}
        for r in store.list_exempt()
    ]


_UNSET = object()


def matches_filters(row, filters):
    name = filters["name"].lower()
    if name and name not in (row.get("name") or "").lower():
        return False
    if filters["label"] != "All" and row.get("label") != filters["label"]:
        return False
    if filters["tracker"] != "All" and row.get("tracker") != filters["tracker"]:
        return False
    return filters["message"] == "All" or message_category(row.get("message")) == filters["message"]


def detection_facets(rows):
    return {
        "labels": sorted({r.get("label") for r in rows if r.get("label")}),
        "trackers": sorted({r.get("tracker") for r in rows if r.get("tracker")}),
        "categories": sorted({message_category(r.get("message")) for r in rows if r.get("message")}),
    }


def add_filter_bar(table, filters, facets, on_change):
    """Add a name input + label/tracker/message dropdowns above a table.

    ``filters`` is the shared dict the table's fetcher reads; ``on_change()``
    is called whenever a filter changes so the table can re-request its page.
    Dropdown options come from ``facets``.
    """

    def set_filter(key, value):
        filters[key] = value
        on_change()

    with table.add_slot("top-left"), ui.row().classes("items-center gap-2 flex-wrap"):
        ui.input("Name", on_change=lambda e: set_filter("name", e.value)).props(
            'outlined dense debounce="300"'
        ).classes("w-52")
        ui.select(
            ["All"] + facets["labels"], value="All", on_change=lambda e: set_filter("label", e.value)
        ).props("outlined dense").classes("w-36")
        ui.select(
            ["All"] + facets["trackers"], value="All", on_change=lambda e: set_filter("tracker", e.value)
        ).props("outlined dense").classes("w-44")
        ui.select(
            ["All"] + facets["categories"], value="All", on_change=lambda e: set_filter("message", e.value)
        ).props("outlined dense").classes("w-52")


def paged_table(columns, fetcher, page_size=25, row_key="id", facets=None, actions=None):
    """Server-side-paginated table: only the visible page is sent to the client.

    ``fetcher(filters, sort_by, descending, page, rows_per_page)`` returns
    ``(rows, total)``. ``facets`` enables the filter bar; ``actions(table)`` may
    add a ``body-cell-actions`` slot. Returns ``(table, load)`` where ``load``
    re-fetches the current page using the current filters.
    """
    table = ui.table(
        columns=columns,
        rows=[],
        row_key=row_key,
        pagination={"rowsPerPage": page_size, "rowsNumber": 0, "page": 1},
    ).classes("w-full")
    current = {"page": 1, "rowsPerPage": page_size, "sortBy": None, "descending": False}
    filters = {"name": "", "label": "All", "tracker": "All", "message": "All"}

    def load(page=_UNSET, rows_per_page=_UNSET, sort_by=_UNSET, descending=_UNSET):
        if page is not _UNSET:
            current["page"] = page or 1
        if rows_per_page is not _UNSET:
            current["rowsPerPage"] = rows_per_page or page_size
        if sort_by is not _UNSET:
            current["sortBy"] = sort_by
        if descending is not _UNSET:
            current["descending"] = bool(descending)
        rows, total = fetcher(
            filters,
            current["sortBy"],
            current["descending"],
            current["page"],
            current["rowsPerPage"],
        )
        table.rows = rows
        table.pagination = {
            "page": current["page"],
            "rowsPerPage": current["rowsPerPage"],
            "rowsNumber": total,
            "sortBy": current["sortBy"],
            "descending": current["descending"],
        }
        table.update()

    def on_request(e: events.GenericEventArguments):
        pagination = (e.args or [{}])[0].get("pagination", {})
        load(
            page=pagination.get("page") or 1,
            rows_per_page=pagination.get("rowsPerPage") or page_size,
            sort_by=pagination.get("sortBy"),
            descending=pagination.get("descending", False),
        )

    table.on("request", on_request)
    if facets:
        add_filter_bar(table, filters, facets, lambda: load(page=1))
    if actions:
        actions(table)
    load()
    return table, load


def focus_refresh(handler):
    """Refresh immediately when the tab regains focus (window focus or visibility change)."""
    bridge = ui.element("div").classes("hidden")
    bridge.on("window_focus", handler)
    ui.run_javascript(
        f"const _bridge = getHtmlElement({bridge.id});"
        "const _fire = () => _bridge.dispatchEvent(new CustomEvent('window_focus', { bubbles: true }));"
        "window.addEventListener('focus', _fire);"
        "document.addEventListener('visibilitychange', () => { if (!document.hidden) _fire(); });"
    )


def _dashboard(store, scanner):
    header("Dashboard")
    settings = store.get_settings()
    dry_run = bool(settings.get("dry_run", True))

    if dry_run:
        banner("DRY RUN - unregistered torrents are detected but NOT removed", "bg-orange-3 text-black")
    else:
        banner("LIVE MODE - unregistered torrents will be removed", "bg-red-3 text-black")

    state = {"table": None, "exempt_table": None, "labels": None, "load": None}

    def refresh_stats():
        current = store.get_settings()
        last_scan_label, stats_label = state["labels"]
        last_scan_label.text = fmt_ts(current.get("last_scan_at")) or "never"
        stats = current.get("last_scan_stats")
        if stats:
            parts = [
                f"total {stats.get('total', 0)}",
                f"unregistered {stats.get('unregistered', 0)}",
            ]
            if stats.get("dry_run"):
                parts.append("dry-run")
            else:
                parts.append(
                    f"removed {stats.get('removed', 0)} +data / {stats.get('removed_nodata', 0)} keep"
                )
            stats_label.text = " | ".join(parts)
        else:
            stats_label.text = ""

    def refresh_detections():
        state["load"]()

    def refresh_exempts():
        state["exempt_table"].rows = exempt_rows(store)
        state["exempt_table"].update()

    def refresh_all():
        refresh_stats()
        refresh_detections()
        refresh_exempts()

    def start_scan():
        if scanner.scanning:
            ui.notify("Scan already in progress", type="warning")
            return
        ui.notify("Scan started", type="info")

        def run():
            try:
                scanner.scan()
            except Exception:
                log.exception("scan failed")

        threading.Thread(target=run, daemon=True).start()

    def row_action(e: events.GenericEventArguments):
        row, action = e.args
        torrent_hash = row["hash"]
        name = row["name"]
        if action == "exempt":
            store.add_exempt(torrent_hash, "exempted from dashboard")
            ui.notify(f"Exempted {name}", type="positive")
            refresh_all()
        elif action == "remove_keep":
            confirm_dialog(
                "Remove torrent",
                f'Remove "{name}" from Deluge keeping files on disk?',
                lambda: do_remove(torrent_hash, name, remove_data=False),
            )
        elif action == "remove_data":
            confirm_dialog(
                "Remove torrent + data",
                f'DELETE files for "{name}" from disk?',
                lambda: do_remove(torrent_hash, name, remove_data=True),
            )

    def do_remove(torrent_hash, name, remove_data):
        try:
            scanner.manual_remove(torrent_hash, remove_data=remove_data)
            ui.notify(f"Removed {name}", type="positive")
        except Exception as exc:
            ui.notify(f"Remove failed: {exc}", type="negative")
        refresh_all()

    def unexempt(torrent_hash):
        store.remove_exempt(torrent_hash)
        ui.notify("Exemption removed", type="positive")
        refresh_exempts()

    with ui.row().classes("items-center gap-4 py-2"):
        ui.label("Last scan:").classes("text-grey")
        last_scan_label = ui.label("never").classes("text-grey")
        stats_label = ui.label().classes("text-grey")
        ui.button("Scan now", icon="refresh").on("click", start_scan)
    state["labels"] = (last_scan_label, stats_label)

    columns = [
        {
            "name": "name",
            "label": "Name",
            "field": "name",
            "sortable": True,
            "align": "left",
            "required": True,
        },
        {"name": "label", "label": "Label", "field": "label", "sortable": True},
        {"name": "tracker", "label": "Tracker", "field": "tracker", "sortable": True},
        {
            "name": "message",
            "label": "Tracker message",
            "field": "message",
            "sortable": True,
            "align": "left",
        },
        {"name": "size", "label": "Size", "field": "size", "sortable": True},
        {"name": "action", "label": "Action", "field": "action", "sortable": True},
        {"name": "actions", "label": "", "field": "actions", "align": "center"},
    ]
    all_rows = detections_rows(store)
    facets = detection_facets(all_rows)

    def fetcher(filters, sort_by, descending, page, rows_per_page):
        rows = [r for r in detections_rows(store) if matches_filters(r, filters)]
        if sort_by:
            rows.sort(key=lambda r: r.get(sort_by) or "", reverse=descending)
        total = len(rows)
        start = (page - 1) * rows_per_page
        return rows[start : start + rows_per_page], total

    def actions(table):
        with table.add_slot("body-cell-actions"), table.cell("actions"):
            ui.button(icon="block").props("size=sm flat").classes("mx-0.5").on(
                "click", js_handler="() => emit(props.row, 'exempt')", handler=row_action
            ).tooltip("Exempt (never touch this torrent)")
            ui.button(icon="link_off").props("size=sm flat").classes("mx-0.5").on(
                "click", js_handler="() => emit(props.row, 'remove_keep')", handler=row_action
            ).tooltip("Remove torrent, keep files")
            ui.button(icon="delete_forever").props("size=sm flat").classes("mx-0.5 text-negative").on(
                "click", js_handler="() => emit(props.row, 'remove_data')", handler=row_action
            ).tooltip("Remove torrent and delete files")

    table, load = paged_table(columns, fetcher, page_size=25, row_key="hash", facets=facets, actions=actions)
    state["table"] = table
    state["load"] = load

    ui.label("Exempt torrents").classes("text-lg font-bold mt-8")
    exempt_table = ui.table(
        columns=[
            {"name": "hash", "label": "Hash", "field": "hash", "sortable": True},
            {"name": "reason", "label": "Reason", "field": "reason", "sortable": True, "align": "left"},
            {"name": "ts", "label": "Added", "field": "ts", "sortable": True},
            {"name": "actions", "label": "", "field": "actions", "align": "center"},
        ],
        rows=exempt_rows(store),
        row_key="hash",
        pagination=10,
    ).classes("w-full mt-2")
    state["exempt_table"] = exempt_table
    with exempt_table.add_slot("body-cell-actions"), exempt_table.cell("actions"):
        ui.button(icon="restore").props("size=sm flat").on(
            "click", js_handler="() => emit(props.row.hash)", handler=lambda e: unexempt(e.args)
        ).tooltip("Remove exemption")

    last_seen = [settings.get("data_version")]

    def tick():
        current = store.get_settings()
        version = current.get("data_version")
        if version != last_seen[0]:
            last_seen[0] = version
            refresh_all()
        elif scanner.scanning:
            refresh_stats()

    focus_refresh(refresh_all)
    ui.timer(3.0, tick)
    refresh_stats()


def _history_row(det):
    return {
        "id": det["id"],
        "ts": fmt_ts(det["ts"]),
        "action": ACTION_LABELS.get(det["action"], det["action"]),
        "name": det["name"],
        "label": det["label"],
        "tracker": det["tracker"],
        "message": det["message"],
        "size": fmt_size(det["size"]),
        "dry_run": "yes" if det["dry_run"] else "no",
    }


def _run_row(det):
    return {
        "id": det["id"],
        "hash": det["torrent_hash"],
        "name": det["name"],
        "seeding": fmt_seeding(det["seeding_time"]),
        "ratio": fmt_ratio(det["ratio"]),
        "label": det["label"],
        "tracker": det["tracker"],
        "message": det["message"],
        "action": ACTION_LABELS.get(det["action"], det["action"]),
        "ts": fmt_ts(det["ts"]),
        "dry_run": "yes" if det["dry_run"] else "no",
    }


def _run_rows(store, run_id):
    return [_run_row(det) for det in store.get_run_detections(run_id)]


def _run(store, run_id):
    header("Run")
    all_rows = _run_rows(store, run_id)
    if not all_rows:
        ui.label(f"Run `{run_id}` - no detections.").classes("text-grey py-2")
        return
    dry = bool(all_rows[0]["dry_run"] == "yes")
    if dry:
        banner("DRY RUN - unregistered torrents were detected but NOT removed", "bg-orange-3 text-black")
    else:
        banner("LIVE MODE - unregistered torrents were removed", "bg-red-3 text-black")
    ui.label(f"Run `{run_id}` - {len(all_rows)} torrents").classes("text-lg font-bold py-2")

    columns = [
        {
            "name": "name",
            "label": "Name",
            "field": "name",
            "sortable": True,
            "align": "left",
            "required": True,
        },
        {"name": "hash", "label": "Hash", "field": "hash", "sortable": True, "align": "left"},
        {"name": "seeding", "label": "Seeded", "field": "seeding", "sortable": True, "align": "left"},
        {"name": "ratio", "label": "Ratio", "field": "ratio", "sortable": True, "align": "left"},
        {"name": "label", "label": "Label", "field": "label", "sortable": True},
        {"name": "tracker", "label": "Tracker", "field": "tracker", "sortable": True},
        {
            "name": "message",
            "label": "Tracker message",
            "field": "message",
            "sortable": True,
            "align": "left",
        },
        {"name": "action", "label": "Action", "field": "action", "sortable": True},
        {"name": "ts", "label": "Time", "field": "ts", "sortable": True},
        {"name": "dry_run", "label": "Dry run", "field": "dry_run", "sortable": True},
    ]
    facets = detection_facets(all_rows)

    def fetcher(filters, sort_by, descending, page, rows_per_page):
        rows = [r for r in all_rows if matches_filters(r, filters)]
        if sort_by:
            rows.sort(key=lambda r: r.get(sort_by) or "", reverse=descending)
        total = len(rows)
        start = (page - 1) * rows_per_page
        return rows[start : start + rows_per_page], total

    paged_table(columns, fetcher, page_size=25, row_key="id", facets=facets)


def _history(store):
    header("History")
    columns = [
        {"name": "ts", "label": "Time", "field": "ts", "sortable": True},
        {"name": "action", "label": "Action", "field": "action", "sortable": True},
        {"name": "name", "label": "Name", "field": "name", "sortable": True, "align": "left"},
        {"name": "label", "label": "Label", "field": "label", "sortable": True},
        {"name": "tracker", "label": "Tracker", "field": "tracker", "sortable": True},
        {"name": "message", "label": "Message", "field": "message", "sortable": True, "align": "left"},
        {"name": "size", "label": "Size", "field": "size", "sortable": True},
        {"name": "dry_run", "label": "Dry run", "field": "dry_run", "sortable": True},
    ]
    facets = store.history_facets()

    def fetcher(filters, sort_by, descending, page, rows_per_page):
        rows, total = store.search_history(
            name=filters["name"],
            label=filters["label"],
            tracker=filters["tracker"],
            message=filters["message"],
            sort_by=sort_by,
            descending=descending,
            page=page,
            rows_per_page=rows_per_page,
        )
        return [_history_row(r) for r in rows], total

    table, load = paged_table(columns, fetcher, page_size=25, row_key="id", facets=facets)
    focus_refresh(load)


def _settings(store, scanner=None):
    header("Settings")
    current = store.get_settings()

    with ui.card().classes("w-full max-w-3xl"):
        ui.label("Server").classes("text-lg font-bold")
        ui.label(
            "Where the web UI and API listen. Saving restarts delugearr automatically "
            "so the new address, port and URL base take effect immediately."
        ).classes("text-sm text-grey")
        bind_host = (
            ui.input("Bind address", value=current.get("host") or "127.0.0.1")
            .props("hint=\"Valid IP address, localhost or '*' for all interfaces\"")
            .classes("w-full max-w-2xl")
        )
        bind_port = ui.number(
            "Port", value=float(current.get("port") or 11012), min=1, max=65535, step=1
        ).classes("w-full max-w-2xl")
        url_base = (
            ui.input(
                "URL base (reverse proxy sub-path)",
                value=current.get("base_path") or "/",
                placeholder="/delugearr",
            )
            .props('hint="Empty or / serves at the root. Must start with a slash."')
            .classes("w-full max-w-2xl")
        )

        def save_server():
            base = (url_base.value or "").strip()
            if base and not base.startswith("/"):
                ui.notify("URL base must start with / or be empty", type="negative")
                return
            try:
                port_value = int(bind_port.value)
            except (TypeError, ValueError):
                port_value = -1
            if not 1 <= port_value <= 65535:
                ui.notify("Port must be between 1 and 65535", type="negative")
                return
            host_value = (bind_host.value or "").strip() or "127.0.0.1"
            store.update_settings(
                host="0.0.0.0" if host_value == "*" else host_value,
                port=port_value,
                base_path=base.rstrip("/") or "/",
            )
            ui.notify("Server settings saved - restarting delugearr...", type="positive")

            def reboot():
                time.sleep(1.0)
                config.restart_app()

            threading.Thread(target=reboot, daemon=True).start()

        ui.button("Save server settings", icon="save", on_click=save_server)

    with ui.card().classes("w-full max-w-3xl mt-4"):
        ui.label("Cleanup behaviour").classes("text-lg font-bold")

        dry_run = ui.switch(
            "Dry run (detect and log only, never remove)", value=bool(current.get("dry_run", True))
        )
        filter_completed = ui.switch(
            "Only process completed torrents (skip active downloads)",
            value=bool(current.get("filter_completed", True)),
        )
        interval = ui.number(
            "Scan interval (minutes)", value=float(current.get("interval_minutes", 30)), min=1, step=1
        )
        grace = ui.number(
            "Grace period (minutes since added, 0 = off)",
            value=float(current.get("grace_minutes", 0)),
            min=0,
            step=1,
        )
        max_per = ui.number(
            "Max removals per tracker per scan (0 = unlimited)",
            value=float(current.get("max_torrents_per_tracker", 0)),
            min=0,
            step=1,
        )
        excluded = ui.input(
            "Excluded labels (comma-separated)", value=", ".join(current.get("excluded_labels") or [])
        )
        keep_paths = ui.input(
            "Keep-data paths - never delete files under these (comma-separated)",
            value=", ".join(current.get("keep_data_paths") or []),
        )
        extra_ignore = ui.input(
            "Extra ignore phrases (comma-separated)", value=", ".join(current.get("extra_ignore") or [])
        )

        for field in (
            dry_run,
            filter_completed,
            interval,
            grace,
            max_per,
            excluded,
            keep_paths,
            extra_ignore,
        ):
            field.classes("w-full max-w-2xl")

        def split_csv(value):
            return [item.strip() for item in value.split(",") if item.strip()]

        def save():
            store.update_settings(
                dry_run=bool(dry_run.value),
                filter_completed=bool(filter_completed.value),
                interval_minutes=max(1, int(interval.value)),
                grace_minutes=max(0, int(grace.value)),
                max_torrents_per_tracker=max(0, int(max_per.value)),
                excluded_labels=split_csv(excluded.value),
                keep_data_paths=split_csv(keep_paths.value),
                extra_ignore=split_csv(extra_ignore.value),
            )
            ui.notify("Settings saved", type="positive")

        ui.button("Save settings", icon="save", on_click=save)

    with ui.card().classes("w-full max-w-3xl mt-4"):
        ui.label("Deluge connection").classes("text-lg font-bold")
        deluge_url = ui.input(
            "Deluge Web URL", value=current.get("deluge_url") or "", placeholder="http://127.0.0.1:8112"
        ).classes("w-full max-w-2xl")
        deluge_password = ui.input(
            "Deluge Web password",
            value=current.get("deluge_password") or "",
            password=True,
            password_toggle_button=True,
        ).classes("w-full max-w-2xl")

        def save_connection():
            store.update_settings(
                deluge_url=(deluge_url.value or "").strip(), deluge_password=deluge_password.value
            )
            ui.notify("Deluge connection saved", type="positive")

        async def test_connection():
            test_label.text = "Testing..."
            test_label.classes(replace="text-grey")
            ok = await run.io_bound(DelugeClient(deluge_url.value, deluge_password.value).connected)
            test_label.text = "Connection: OK" if ok else "Connection: failed"
            test_label.classes(replace="text-positive" if ok else "text-negative")

        with ui.row().classes("items-center gap-4"):
            ui.button("Save connection", icon="save", on_click=save_connection)
            ui.button("Test connection", icon="wifi_tethering", on_click=test_connection)
            test_label = ui.label().classes("text-grey")

        if scanner is not None:
            connected = scanner.deluge_ok
            state = {True: "connected", False: "unreachable", None: "not yet probed"}.get(connected)
            color = {True: "text-positive", False: "text-negative", None: "text-grey"}[connected]
            ui.label(f"Last known status: {state}").classes(f"text-sm {color}")

    with ui.card().classes("w-full max-w-3xl mt-4"):
        ui.label("Security").classes("text-lg font-bold")
        ui.label(
            "Skip the login page for requests coming from trusted networks "
            "(e.g. your Tailscale subnet). The REST API always requires the API key."
        ).classes("text-sm text-grey")

        bypass = ui.switch(
            "Bypass login for trusted networks",
            value=bool(current.get("auth_bypass_enabled", False)),
        ).classes("w-full max-w-2xl")

        trusted_networks = ui.input(
            "Trusted networks - bypass login (comma-separated CIDRs)",
            value=", ".join(current.get("trusted_networks") or []),
        ).classes("w-full max-w-2xl")
        trusted_networks.props(
            'hint="e.g. 100.64.0.0/10 (Tailscale IPv4), fd7a:115c:a1e0::/48 (Tailscale IPv6). Localhost is trusted by default."'
        )

        trusted_proxies = ui.input(
            "Trusted proxies - may set X-Forwarded-For (comma-separated CIDRs)",
            value=", ".join(current.get("trusted_proxies") or []),
        ).classes("w-full max-w-2xl")
        trusted_proxies.props(
            'hint="Only requests from these peers are allowed to set X-Forwarded-For. Set your reverse proxy here."'
        )

        ui.separator().classes("my-2")
        ui.label("API key").classes("text-lg font-bold")
        ui.label(
            "The API always requires this key via the X-Api-Key header (or ?apikey= query)"
            f" on every /api request. See {config.base_path()}/api/docs for the spec."
        ).classes("text-sm text-grey")
        api_key = store.api_key()
        with ui.row().classes("items-center gap-2 w-full"):
            key_input = ui.input("API key", value=api_key).props("readonly").classes("flex-1")
            ui.button(icon="content_copy").props("flat").tooltip("Copy API key").on(
                "click",
                lambda: (ui.clipboard.write(key_input.value), ui.notify("API key copied", type="positive")),
            )

        def save_security():
            store.update_settings(
                auth_bypass_enabled=bool(bypass.value),
                trusted_networks=split_csv(trusted_networks.value),
                trusted_proxies=split_csv(trusted_proxies.value),
            )
            ui.notify("Security settings saved", type="positive")

        def do_regenerate():
            key_input.value = store.regenerate_api_key()
            key_input.update()
            ui.notify("API key regenerated", type="positive")

        with ui.row().classes("items-center gap-4"):
            ui.button("Save security settings", icon="save", on_click=save_security)
            ui.button(
                "Regenerate key",
                icon="refresh",
                on_click=lambda: confirm_dialog(
                    "Regenerate API key",
                    "The current key will stop working immediately. Continue?",
                    do_regenerate,
                ),
            )

    notifications_card(store)


def notifications_card(store):
    """Sonarr/Radarr-style Notifications settings section."""
    with ui.card().classes("w-full max-w-3xl mt-4"):
        ui.label("Notifications").classes("text-lg font-bold")
        ui.label(
            "Send notifications via Discord webhooks or ntfy for the events you choose. "
            "Summary messages are capped to one per scan so a big cleanup never floods the channel."
        ).classes("text-sm text-grey")

        cap = ui.number(
            "Max torrents listed per summary (0 = summary only)",
            value=float(store.get_settings().get("notify_max_items", 25) or 25),
            min=0,
            step=1,
        ).classes("w-full max-w-2xl")

        url_base = ui.input(
            "Public UI base URL (for notification run links)",
            value=store.get_settings().get("notify_url_base") or "",
            placeholder="https://delugearr.example.com",
        ).classes("w-full max-w-2xl")

        tvdb_key = (
            ui.input("TVDB API key (optional, for notification artwork)")
            .props(
                'type="password" hint="Shows the TVDB show banner on deletion notifications, matching qbit-manage."'
            )
            .classes("w-full max-w-2xl")
        )
        tvdb_key.value = store.get_settings().get("tvdb_api_key") or ""
        shown = {"state": False}
        with tvdb_key.add_slot("append"):
            toggle_icon = ui.icon("visibility").classes("cursor-pointer")

            def toggle_key():
                shown["state"] = not shown["state"]
                tvdb_key.props(f"type={'text' if shown['state'] else 'password'}")
                toggle_icon.name = "visibility_off" if shown["state"] else "visibility"

            toggle_icon.on("click", toggle_key)

        artwork = ui.switch(
            "Show TVDB artwork on deletion notifications",
            value=bool(store.get_settings().get("notify_artwork", False)),
        ).classes("w-full max-w-2xl")

        def save_settings():
            store.update_settings(
                notify_max_items=max(0, int(cap.value)),
                notify_url_base=(url_base.value or "").strip(),
                tvdb_api_key=(tvdb_key.value or "").strip(),
                notify_artwork=artwork.value,
            )
            ui.notify("Settings saved", type="positive")

        def open_dialog(conn):
            edit = conn is not None
            conn = conn or {
                "name": "",
                "type": "discord",
                "webhook_url": "",
                "username": "",
                "avatar": "",
                "access_token": "",
                "triggers": [],
            }
            dialog = ui.dialog()
            with dialog, ui.card().classes("w-96"):
                ui.label("Edit connection" if edit else "Add connection").classes("text-lg font-bold")
                name = ui.input("Name", value=conn.get("name") or "").classes("w-full")
                conn_type = ui.select(
                    {"discord": "Discord", "ntfy": "ntfy"},
                    label="Type",
                    value=conn.get("type") or "discord",
                ).classes("w-full")

                webhook = ui.input("Webhook URL", value=conn.get("webhook_url") or "").classes("w-full")
                webhook_link = ui.link(
                    "How to create a Discord webhook",
                    "https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks",
                    new_tab=True,
                ).classes("text-xs text-grey -mt-1")
                username = (
                    ui.input("Username (optional)", value=conn.get("username") or "")
                    .props('hint="The username to post as. Defaults to the Discord webhook name."')
                    .classes("w-full")
                )
                avatar = (
                    ui.input("Avatar URL (optional)", value=conn.get("avatar") or "")
                    .props(
                        'hint="The avatar to use for messages. Defaults to the avatar you set when creating the webhook."'
                    )
                    .classes("w-full")
                )
                access_token = (
                    ui.input("Access token (optional)", value=conn.get("access_token") or "")
                    .props('hint="Required only if your ntfy server requires auth. Sent as a Bearer token."')
                    .classes("w-full")
                )
                ui.label("Select which events should trigger this notification").classes(
                    "text-sm font-medium pt-2"
                )
                toggles = {
                    t: ui.switch(label, value=t in (conn.get("triggers") or []))
                    for t, label in TRIGGER_LABELS.items()
                }
                result_label = ui.label("Saving verifies the webhook first.").classes("text-sm text-grey")
                with ui.row().classes("gap-2 pt-2 w-full"):
                    ui.button("Cancel", on_click=dialog.close)
                    test_btn = ui.button("Test", on_click=lambda: run_test())
                    ui.button("Save", color="primary", on_click=lambda: save(dialog, conn))
            dialog.open()

            def apply_type(kind):
                """Re-label/hide fields depending on the selected channel type."""
                discord = kind == "discord"
                webhook.props(
                    f'hint="{"Create a webhook in your Discord server and paste its URL here." if discord else "Paste your ntfy topic publish URL, e.g. https://ntfy.sh/mytopic."}"'
                )
                webhook_link.set_visibility(discord)
                username.set_visibility(discord)
                avatar.set_visibility(discord)
                access_token.set_visibility(not discord)

            apply_type(conn_type.value)
            conn_type.on_value_change(lambda e: apply_type(e.value))

            def invalidate():
                result_label.text = "Webhook details changed"
                result_label.classes(replace="text-sm text-grey")

            for field in (webhook, username, avatar, access_token):
                field.on_value_change(lambda _e: invalidate())

            def build_conn():
                return {
                    "type": conn_type.value,
                    "webhook_url": (webhook.value or "").strip(),
                    "username": username.value,
                    "avatar": avatar.value,
                    "access_token": (access_token.value or "").strip(),
                }

            def run_test():
                url = (webhook.value or "").strip()
                if not url:
                    ui.notify("Webhook URL required to test", type="warning")
                    return
                result_label.text = "Testing..."
                result_label.classes(replace="text-sm text-grey")
                test_btn.disable()
                ok = verify_webhook()
                if ok:
                    result_label.text = "Test successful"
                    result_label.classes(replace="text-sm text-positive")
                else:
                    result_label.text = "Test failed"
                    result_label.classes(replace="text-sm text-negative")
                test_btn.enable()

            def verify_webhook():
                data = build_conn()
                if not data["webhook_url"]:
                    return False
                try:
                    return make_notifier(data).send_test()
                except Exception as exc:
                    log.warning("notification test failed: %s", exc)
                    return False

            def save(dialog, conn):
                data = build_conn()
                if not data["webhook_url"]:
                    ui.notify("Webhook URL required to save", type="warning")
                    return
                if not verify_webhook():
                    result_label.text = "Webhook failed verification - not saved"
                    result_label.classes(replace="text-sm text-negative")
                    ui.notify("Connection failed verification - not saved", type="negative")
                    return
                payload = {
                    "name": name.value or data["type"],
                    "type": data["type"],
                    "webhook_url": data["webhook_url"],
                    "username": data["username"],
                    "avatar": data["avatar"],
                    "access_token": data["access_token"],
                    "triggers": [t for t, s in toggles.items() if s.value],
                }
                if edit:
                    store.update_notification(conn["id"], **payload)
                else:
                    store.add_notification(**payload)
                dialog.close()
                refresh_list()
                ui.notify("Saved", type="positive")

        def delete(conn):
            confirm_dialog(
                "Delete connection",
                f'Delete "{conn["name"]}"?',
                lambda: (store.delete_notification(conn["id"]), refresh_list()),
            )

        def render_connection(conn):
            with list_container, ui.row().classes("items-center gap-3 w-full"):
                enabled = ui.switch(value=conn["enabled"]).props("dense")
                enabled.on_value_change(lambda e, c=conn["id"]: store.update_notification(c, enabled=e.value))
                ui.label(conn["name"] or "(unnamed)").classes("flex-1")
                ui.badge(conn.get("type") or "discord").classes("text-xs").props("outline").tooltip(
                    conn.get("type") or "discord"
                )
                ui.button(icon="edit").props("flat size=sm").tooltip("Edit").on(
                    "click", lambda c=conn: open_dialog(c)
                )
                ui.button(icon="delete").props("flat size=sm text-negative").tooltip("Delete").on(
                    "click", lambda c=conn: delete(c)
                )

        def refresh_list():
            list_container.clear()
            with list_container:
                for conn in store.list_notifications():
                    render_connection(conn)

        with ui.row().classes("items-center gap-4"):
            ui.button("Save settings", icon="save", on_click=save_settings)
            ui.button("Add connection", icon="add", on_click=lambda: open_dialog(None))

        list_container = ui.column().classes("w-full mt-2")
        refresh_list()


def _login(redirect_to: str = "/"):
    setup_theme()
    if app.storage.user.get("authenticated"):
        ui.navigate.to("/")
        return

    def try_login():
        if not config.auth_password():
            ui.notify("AUTH_PASSWORD is not set", color="negative")
            return
        if username.value == config.auth_user() and password.value == config.auth_password():
            app.storage.user.update(username=username.value, authenticated=True)
            ui.navigate.to(redirect_to or "/")
        else:
            ui.notify("Wrong username or password", color="negative")

    with ui.card().classes("absolute-center items-stretch w-96"):
        with ui.column().classes("items-center gap-2 mb-4"):
            ui.image(DEFAULT_AVATAR).classes("w-16 h-16 rounded-full")
            ui.label("Delugearr").classes("text-2xl font-bold")
        username = (
            ui.input("Username")
            .props("autofocus autocomplete=username")
            .classes("w-full")
            .on("keydown.enter", lambda: password.run_method("focus"))
        )
        password = (
            ui.input("Password", password=True, password_toggle_button=True)
            .props("autocomplete=current-password")
            .classes("w-full")
            .on("keydown.enter", try_login)
        )
        ui.button("Log in", on_click=try_login, icon="login").classes("w-full mt-2")


def build_pages(store, scanner):
    @ui.page("/login")
    def login(redirect_to: str = "/"):
        _login(redirect_to)

    @ui.page("/")
    def dashboard():
        _dashboard(store, scanner)

    @ui.page("/history")
    def history():
        _history(store)

    @ui.page("/run/{run_id}")
    def run_page(run_id: str):
        _run(store, run_id)

    @ui.page("/settings")
    def settings_page():
        _settings(store, scanner)

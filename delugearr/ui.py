"""Delugearr UI pages (NiceGUI)."""

import logging
import threading
from datetime import datetime

from nicegui import events, ui

from . import config

log = logging.getLogger("delugearr-ui")

ACTION_LABELS = {
    "would_remove_data": "Would remove + data",
    "would_remove_only": "Would remove (keep data)",
    "removed_data": "Removed + data",
    "removed_only": "Removed (kept data)",
    "manual_removed_data": "Manually removed + data",
    "manual_removed_only": "Manually removed (kept data)",
    "error": "Failed",
}


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


def header(current):
    base = config.base_path()
    pages = [
        ("Dashboard", base + "/"),
        ("History", base + "/history"),
        ("Settings", base + "/settings"),
    ]
    with ui.header().classes("items-center px-4 py-2"), ui.row().classes("items-center gap-6"):
        ui.label("Delugearr").classes("text-xl font-bold")
        with ui.row().classes("gap-4"):
            for name, url in pages:
                classes = "text-white font-bold" if name == current else "text-white/60"
                ui.link(name, url).classes(classes)


def detections_rows(store):
    rows = []
    latest = store.latest_run()
    if latest:
        for det in store.get_run_detections(latest["run_id"]):
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


def confirm_dialog(title, message, on_confirm):
    dialog = ui.dialog()
    with dialog, ui.card():
        ui.label(title).classes("text-lg font-bold")
        ui.label(message)
        with ui.row().classes("gap-2 pt-2"):
            ui.button("Cancel", on_click=dialog.close)
            ui.button("Confirm", on_click=lambda: (on_confirm(), dialog.close())).props("color=negative")
    dialog.open()


def banner(text, color):
    ui.label(text).classes(f"w-full text-center font-bold py-2 px-4 {color}")


def _dashboard(store, scanner):
    header("Dashboard")
    settings = store.get_settings()
    dry_run = bool(settings.get("dry_run", True))

    if dry_run:
        banner("DRY RUN - unregistered torrents are detected but NOT removed", "bg-orange-3 text-black")
    else:
        banner("LIVE MODE - unregistered torrents will be removed", "bg-red-3 text-black")

    state = {"table": None, "exempt_table": None, "labels": None}

    def refresh_stats():
        current = store.get_settings()
        last_scan_label, stats_label = state["labels"]
        last_scan_label.text = fmt_ts(current.get("last_scan_at")) or "never"
        stats = current.get("last_scan_stats")
        if stats:
            parts = [
                f"total {stats.get('total', 0)}",
                f"unregistered {stats.get('unregistered', 0)}",
                f"transient {stats.get('transient', 0)}",
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
        state["table"].rows = detections_rows(store)
        state["table"].update()

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
    table = ui.table(columns=columns, rows=detections_rows(store), row_key="hash", pagination=25).classes(
        "w-full"
    )
    state["table"] = table
    with table.add_slot("top-left"):
        ui.input("Filter...").props('outlined dense debounce="300"').bind_value(table, "filter").classes(
            "w-72"
        )
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

    last_seen = [None]

    def tick():
        current = store.get_settings()
        last_scan_at = current.get("last_scan_at")
        if last_seen[0] != last_scan_at:
            last_seen[0] = last_scan_at
            refresh_all()
        elif scanner.scanning:
            refresh_stats()

    ui.timer(3.0, tick)
    refresh_stats()


def _history(store):
    header("History")
    rows = []
    for det in store.get_detections(1000):
        rows.append(
            {
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
        )
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
    table = ui.table(columns=columns, rows=rows, row_key="id", pagination=25).classes("w-full")
    with table.add_slot("top-left"):
        ui.input("Filter...").props('outlined dense debounce="300"').bind_value(table, "filter").classes(
            "w-72"
        )


def _settings(store):
    header("Settings")
    current = store.get_settings()

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

    for field in (dry_run, filter_completed, interval, grace, max_per, excluded, keep_paths, extra_ignore):
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


def build_pages(store, scanner):
    @ui.page("/")
    def dashboard():
        _dashboard(store, scanner)

    @ui.page("/history")
    def history():
        _history(store)

    @ui.page("/settings")
    def settings_page():
        _settings(store)

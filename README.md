# delugearr

Detect and clean up **"unregistered" torrents** on the seedbox's Deluge — torrents the tracker reports as no longer registered (deleted/trumped/nuked) — and remove them, with a NiceGUI web UI.

Built to mirror qbit_manage's `rem_unregistered` behavior for Deluge. The `delugearr` package also hosts the scanning/scheduling backend so future Deluge management features can be added.

## What it does

- Reads each torrent's `tracker_status` from Deluge Web JSON-RPC (`127.0.0.1:10376`).
- Matches the same tracker-message lists qbit_manage uses (`TorrentMessages`): `Unregistered torrent`, `Torrent has been deleted`, `not registered with this tracker`, `InfoHash not found`, plus BeyondHD deletion reasons (`Trumped`/`Dupe`/`Nuked`/`Complete Season Uploaded`).
- Guards out transient errors (`timed out`, `Host not found`, `Bad Gateway`, `stream truncated`, passkey issues, …) — those are never removed.
- When matched, removes the torrent **and its data** — including cross-seed path torrents (their data is hardlinked, so originals survive). A `keep_data_paths` setting is available as an opt-in safety net.
- **Dry run is ON by default** (`DRY_RUN=1`): detect + log only. Flip it in Settings (or the env var) to actually remove.

## Web UI

Served at `https://seedbox.savagecore.uk/delugearr` (basic auth, no subdomain):

- **Dashboard** — latest scan's unregistered torrents with sortable/filterable/paginated table, per-row Exempt / Remove·keep-data / Remove+data, exempt list, scan-now button. Torrents you remove or exempt disappear from the list immediately (they still live in History).
- **History** — audit log of detections/removals (also sortable/filterable).
- **Settings** — dry-run toggle, scan interval, grace period, per-tracker removal cap, excluded labels, keep-data paths, extra ignore phrases, Deluge connection (URL/password + test button), and the API key (view, copy, regenerate).

## API

Every `/api` endpoint requires the API key, sent either as the `X-Api-Key` header or the `apikey` query parameter (the arr convention). The key is generated on first run and managed in **Settings → API** (regenerating it instantly invalidates the old key). `/api/health`, `/api/docs` and `/api/openapi.json` are unauthenticated.

- Interactive spec UI: `https://seedbox.savagecore.uk/delugearr/api/docs`
- Machine-readable spec (for the MCP server): `https://seedbox.savagecore.uk/delugearr/api/openapi.json`

```bash
KEY=your-api-key
curl -H "X-Api-Key: $KEY" http://127.0.0.1:11012/delugearr/api/status
curl -X POST -H "X-Api-Key: $KEY" http://127.0.0.1:11012/delugearr/api/scan
curl http://127.0.0.1:11012/delugearr/api/health   # liveness, no key
```

Endpoints: `health`, `status`, `scan`, `detections` (latest run + filters), `history`, `torrents/{hash}/remove`, `torrents/{hash}/exempt`, `exempt` (GET/DELETE), `settings` (GET/PUT — the Deluge password and API key are never returned).

## Development

```bash
python -m venv venv && . venv/bin/activate
pip install -e ".[dev]"
make lint      # ruff check + format --check
make test      # pytest
make install-hooks  # lefthook git hooks (lint + conventional commits)
```

## Deploy

```bash
DELUGE_PASSWORD='...' ./deploy.sh
```

`deploy.sh` rsyncs to `/opt/delugearr`, installs the venv, writes `/etc/delugearr/config` (chmod 600), installs the `delugearr.service` systemd unit and the nginx site block (`/etc/nginx/apps/delugearr.conf`), and removes the legacy `deluge-unregistered` service. See `config.example` for the environment keys.

## Releasing

Tag a `v*` tag; the release workflow lints/tests and publishes a GitHub release with a wheel. Deploys remain local-driven.

## How it works

| Module | Purpose |
| --- | --- |
| `delugearr/deluge_client.py` | Thin Deluge Web JSON-RPC client |
| `delugearr/detector.py` | qbit_manage-ported tracker-message matching |
| `delugearr/scanner.py` | Scan cycle (fetch, detect, remove, audit) |
| `delugearr/store.py` | SQLite settings / detections / exempt list |
| `delugearr/ui.py` | NiceGUI pages |
| `delugearr/api.py` | API-key-protected REST API (OpenAPI spec) |
| `delugearr/app.py` | FastAPI + NiceGUI mount (`/delugearr`) + scheduler |

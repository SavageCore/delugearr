# delugearr

<p align="center">
  <img src="Logo/256.png" alt="Delugearr" width="128" height="128" />
</p>

Detect torrents that a tracker reports as **unregistered** (deleted, trumped,
nuked) and remove them. NiceGUI web UI, *arr-style API.

Built to mirror qbit_manage's `rem_unregistered` behaviour for Deluge. The
`delugearr` package also hosts the scanning/scheduling backend, so further
Deluge management features can be added.

## What it does

- Reads each torrent's `tracker_status` from the Deluge Web JSON-RPC API.
- Matches the same tracker-message lists qbit_manage uses (`TorrentMessages`):
  `Unregistered torrent`, `Torrent has been deleted`, `not registered with this
  tracker`, `InfoHash not found`, plus BeyondHD deletion reasons (`Trumped`,
  `Dupe`, `Nuked`, `Complete Season Uploaded`).
- Guards out transient tracker errors (`timed out`, `Host not found`,
  `Bad Gateway`, `stream truncated`, passkey problems and similar). Those are
  never removed.
- When matched, removes the torrent **and its data**. To protect specific
  directories, list them under **Settings > Keep-data paths**: torrents saved
  under those paths are removed from Deluge, but their files stay on disk.
- **Dry run is ON by default** (`DRY_RUN=1`): detect and log only. Turn it off
  in Settings (or via the env var) once you trust what it is finding.

## Quick start

```bash
git clone https://github.com/SavageCore/delugearr && cd delugearr
uv sync
CONFIG_PATH=./config AUTH_USER=admin AUTH_PASSWORD='choose-one' uv run python -m delugearr
```

Open `http://127.0.0.1:11012`, log in, and point it at Deluge in
**Settings** (URL and Web UI password, defaults to `http://127.0.0.1:8112`).
Scan interval, dry run and exclusions are all editable there too.

- `CONFIG_PATH` (default `/etc/delugearr`) holds the SQLite database, the log
  and the session secret.
- `AUTH_USER` / `AUTH_PASSWORD` are the web UI login. Set them: without a
  password nobody can log in.
- The server listens on `127.0.0.1:11012` at the root path. Set `HOST=*` (or
  `0.0.0.0`) to expose it on the network, or leave it local and put a reverse
  proxy in front. To serve it under a sub-path (e.g. `/delugearr`), set
  `BASE_PATH`. These are also editable in **Settings > Server**; saving restarts
  the app automatically so the change takes effect (Sonarr-style).
- To reach the UI over Tailscale **without** the shared (e.g. nginx basic-auth)
  entry point, `deploy.sh` installs `nginx-delugearr-tailscale.conf` — a vhost
  bound to the seedbox's Tailscale IP with a Tailscale TLS cert, with no basic
  auth. Give clients on the tailnet trusted access so they skip the app login:
  `AUTH_BYPASS_ENABLED=1` with `TRUSTED_NETWORKS=["100.64.0.0/10",
  "fd7a:115c:a1e0::/48"]` covers the whole tailnet; the REST API still always
  requires the API key. These are also editable in **Settings > Security**.
- `config.example` lists every environment variable. They seed the initial
  settings; after first run the UI is the source of truth.

## Web UI

- **Dashboard**: the latest scan's unregistered torrents in a sortable,
  filterable, paginated table, with per-row Exempt / Remove keep-data /
  Remove + data, the exempt list, and a scan-now button. Torrents you remove or
  exempt disappear from the list immediately (they stay in History).
- **History**: audit log of detections and removals, also sortable and
  filterable.
- **Settings**: dry-run toggle, scan interval, grace period, unregistered
  confirmation dwell timer, per-tracker removal cap, excluded labels,
  keep-data paths, extra ignore phrases, the Deluge connection (URL, password,
  test button) and the API key (view, copy, regenerate).
- **Notifications**: Sonarr/Radarr-style connections with a name, a webhook /
  topic URL, per-event toggles (scan summary, per-torrent removals, errors,
  manual actions) and a test button, supporting two channels:
  - **Discord**: webhook URL, optional username and avatar override.
  - **ntfy** (ntfy.sh or self-hosted): the topic publish URL, e.g.
    `https://ntfy.sh/mytopic`, plus an optional access token sent as a Bearer
    header when the server requires auth.
  Scan summaries cap how many torrent names they list (25 by default) so a big
  cleanup posts one message instead of flooding the channel. Per-torrent removal
  notifications use a qbit-manage-style Discord embed with Contents Deleted,
  Status, Category, Tag, Tracker and a code-blocked torrent name; when a
  `TVDB_API_KEY` is configured (see `config.example`), the show's TVDB banner is
  attached too.

## API

`/api/health`, `/api/docs` and `/api/openapi.json` are open. Every other `/api`
endpoint needs the API key, sent as the `X-Api-Key` header or the `apikey`
query parameter (the *arr convention). The key is generated on first run and
lives in **Settings > API**; regenerating it invalidates the old one
immediately.

```bash
KEY=your-api-key
curl -H "X-Api-Key: $KEY" http://127.0.0.1:11012/api/status
curl -X POST -H "X-Api-Key: $KEY" http://127.0.0.1:11012/api/scan
curl http://127.0.0.1:11012/api/health   # liveness, no key
```

Interactive spec at `/api/docs`, machine-readable spec (for MCP
clients) at `/api/openapi.json`. The `/api` prefix is relative to the mount
path: at the root by default, or under `BASE_PATH` when set.

Endpoints: `health`, `status`, `scan`, `detections` (latest run plus filters),
`history`, `torrents/{hash}/remove`, `torrents/{hash}/exempt`, `exempt`
(GET/DELETE), `settings` (GET/PUT, the Deluge password and API key are never
returned), and `notifications` (GET/POST/PUT/DELETE plus `{id}/test`, webhook
URLs are redacted).

## Development

```bash
uv sync --dev
uv run make lint           # ruff check + format --check
uv run make test           # pytest
make install-hooks  # lefthook git hooks (lint + conventional commits)
```

| Module | Purpose |
| --- | --- |
| `delugearr/deluge_client.py` | Thin Deluge Web JSON-RPC client |
| `delugearr/detector.py` | qbit_manage-ported tracker-message matching |
| `delugearr/scanner.py` | Scan cycle (fetch, detect, remove, audit, notify) |
| `delugearr/store.py` | SQLite settings / detections / exempt / notifications |
| `delugearr/notifier.py` | Discord webhook and ntfy notifications (capped summaries) |
| `delugearr/ui.py` | NiceGUI pages |
| `delugearr/api.py` | API-key-protected REST API (OpenAPI spec) |
| `delugearr/app.py` | FastAPI + NiceGUI mount (`/delugearr`) + scheduler |

## Running as a service

`delugearr.service.example` is an example systemd unit: copy it to
`delugearr.service`, edit `User`, `Group` and the paths, drop it in
`/etc/systemd/system/`, and point `EnvironmentFile` at your config. If you
expose the app beyond localhost, put a reverse proxy with TLS in front of it.

Tag a `v*` tag to cut a release: the workflow lints, tests and publishes a
GitHub release with a wheel.

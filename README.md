# CrowdRoom

![Python](https://img.shields.io/badge/Python-3.14+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![Vue](https://img.shields.io/badge/Vue-3-4FC08D?logo=vuedotjs&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-supported-4169E1?logo=postgresql&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)
![License](https://img.shields.io/badge/License-AGPL%203.0-E44BC2?logo=gnu&logoColor=white)

CrowdRoom lets a group request songs in a shared room while the host controls playback on their Spotify account. FastAPI and Vue 3 keep the queue and room state in sync over WebSockets.

Audio plays on the host's Spotify device: an iPhone, a computer running Spotify, or a compatible speaker. CrowdRoom does not stream audio into each listener's browser. A NAS can host the app without a browser or audio output; the playback device runs elsewhere.

## Use a room

### Host

1. Register and log in, then create a room. Use a private room with a PIN or share an invite when access should be limited.
2. Open **Connection** and authorize the Spotify account that will supply the music. Spotify playback control requires Premium.
3. Open Spotify on the playback device using that same account. For an iPhone, keep Spotify online and open it if the device is missing from the list. If necessary, start a song in Spotify once to make its Connect session available.
4. Select that device in CrowdRoom. Only the room host can choose or change devices.
5. Share the room link, code, or QR code. Review first-entry requests from registered users; compare their copyable **CrowdRoom ID**, such as `CR-4827-9163`, when confirming identity in a voice room.
6. Add songs and use Play, Pause, and Skip to control the selected device. A missing or unavailable device needs to be restored before playback can continue.

You can authorize Spotify before creating a room. Inside somebody else's room, only that room's host manages the music connection. Profile and Connection are separate pages.

Spotify authorization is stored encrypted in the database, and access tokens are refreshed using the stored refresh token. Keep the database and encryption key to retain this authorization. Spotify may still require authorization again if permission is revoked or a token becomes invalid; device availability is a separate Spotify Connect state.

Spotify can temporarily limit API requests. CrowdRoom shares the returned retry deadline across playback, device discovery, search, and token requests for that Spotify app, and keeps it across restarts. The room shows a countdown and disables affected controls while waiting; authorization remains connected. Existing music can continue playing, but playback updates cannot be refreshed until Spotify permits requests again.

During that cooldown, saved songs remain searchable and can still be added to the local queue without a Spotify request. Registered hosts, administrators, and members have no guest request quota. Guests retain their five-minute first-entry wait, thirty-minute request interval, and one-waiting-song limit. Online results and playback commands still wait for Spotify.

CrowdRoom follows playback, pause, track changes, and seeks made on the host's selected Spotify device. Observing a phone track change preserves waiting songs. After a restart, monitoring resumes without issuing a Play command; paused and idle sessions poll less often.

### Registered member

1. Log in and open the room link, enter the room code, or scan its QR code.
2. Enter the PIN if required. On the first visit to a room, contact its host or an administrator for approval. Your waiting prompt includes your permanent CrowdRoom ID and a copy button.
3. Once approved, join and request songs. Future visits with the same account do not need another approval. The guest request quota does not apply to registered members.

### Guest

1. Select **Join as a guest** on the login page, enter a nickname, and choose **Continue as guest**. No email or password is needed.
2. Join a room without approval. Private-room credentials, invite validity, capacity limits, and room bans still apply.
3. Wait **5 minutes after your first successful room entry** before requesting your first song. After each successful request, wait **30 minutes** before the next one. You can have at most **one waiting song** in the queue.

The server stores the waiting period and request cooldown against your guest identity across rooms. Refreshing, changing your nickname, leaving, or rejoining does not reset them. The browser retains a protected guest identity cookie; keep browser data to keep that identity. Guests cannot create rooms, become administrators, manage Spotify connections, or choose playback devices.

Source rate limits help prevent rapid creation of many identities. IP addresses are an auxiliary signal: users on the same Wi-Fi do not share one song quota. Clearing browser data can create a new identity, and changing networks can evade IP limits. These controls cannot prove that two guests are different people.

### Queue and moderation

- Only the host grants or removes administrators. Administrators manage ordinary members and cannot act on the host or other administrators.
- Host and administrators can use **Jump**, **Pin**, and **Dequeue**. Ordinary members cannot remove pending songs.
- **Jump** leaves the current song playing and moves a song ahead of ordinary queued songs. Pinned songs retain priority.
- **Pin** adds a song after existing pinned songs. Pinned songs play from top to bottom; unpinning moves a song to the front of the ordinary queue. Songs leave the queue after playback.
- Votes do not reorder pinned songs or overtake a song moved with Jump.
- A naturally empty queue starts again when a song is added. Adding songs after a deliberate Pause keeps playback paused until a manager resumes it.
- **Kick** removes a member, who can join again subject to room access rules. **Ban** blocks that account from the room until a manager unbans it. Rejoining never restores an earlier administrator role.
- Managers can see whether the host has authorized Spotify. Ordinary members do not see this connection status. Device discovery and selection are reserved for the host.

### HelloTalk and speakers

For a HelloTalk VoiceRoom, the host creates both rooms, authorizes Spotify, opens the same Spotify account on their iPhone, selects the iPhone in CrowdRoom, and shares the CrowdRoom link for song requests. CrowdRoom then controls the iPhone's queue playback.

A Bluetooth speaker can play the iPhone's output. Neither phone nor speaker output proves that HelloTalk sends the music to remote listeners. That audio path must be checked separately with the actual phone, HelloTalk version, and audio setup.

## Docker setup

Use the source checkout containing these features. The default Compose override builds this checkout; running only `docker-compose.yml` uses an upstream image that may not contain this branch's changes.

### Configure

```bash
cp .env.example .env
chmod 600 .env
docker compose build app
```

Edit `.env` and replace the signing key, encryption key, and database password placeholders. Generate random signing keys and database passwords with `openssl rand -hex 32`. Generate the Fernet encryption key from the built image:

```bash
docker compose run --rm --no-deps --entrypoint python app -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Set the same database password in `POSTGRES_PASSWORD` and `DATABASE_URL`. Keep `SECRET_KEY` and `ENCRYPTION_KEY` stable across restarts, upgrades, and moves to a NAS. Changing the signing key invalidates sessions; losing the encryption key prevents reading existing Spotify credentials. Keep `.env` and backups private.

For a local computer, the template uses:

```env
BIND_IP=127.0.0.1
HOST_PORT=8000
FRONTEND_URL=http://127.0.0.1:8000
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8000/api/v1/auth/spotify/callback
COOKIE_SECURE=False
```

Set `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` from your Spotify app, and register the exact `SPOTIFY_REDIRECT_URI` in its developer dashboard. When changing the host port, update both URLs and the registered callback. Spotify requires HTTPS except for explicit loopback IP addresses; `localhost` is not accepted as a redirect URI. See [Spotify's redirect URI requirements](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri).

### Let phones join on the same Wi-Fi

A phone cannot reach the computer through `127.0.0.1`: that address points back to the phone. For a local Docker installation, expose the app to the LAN and set a separate public origin for QR and copied invite links:

```env
BIND_IP=0.0.0.0
HOST_PORT=8000
PUBLIC_URL=http://192.168.1.20:8000
FRONTEND_URL=http://127.0.0.1:8000
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8000/api/v1/auth/spotify/callback
COOKIE_SECURE=False
```

Replace the example IP with the computer's current LAN address and keep the port consistent. Recreate the app container after changing `.env`, then generate a new QR code. Check the public URL from the phone first; the computer firewall or Wi-Fi client isolation can still block LAN access. Reserve the computer's LAN address in DHCP or update `PUBLIC_URL` if that address changes.

The host should keep opening CrowdRoom through `FRONTEND_URL` when authorizing Spotify, so the authorization and callback use the same browser host and cookies. Guests use `PUBLIC_URL`; their sessions on the LAN address are separate from sessions on the loopback address. `PUBLIC_URL` must be an HTTP(S) origin without a path, credentials, query, or fragment. If empty, shared links use `FRONTEND_URL` as before. It does not change the Spotify callback or bypass room access rules.

### First start with an empty database

Run migrations before starting the app, so Alembic records the schema from the beginning:

```bash
docker compose up -d db
docker compose run --rm --no-deps --entrypoint alembic app upgrade head
docker compose up -d app
docker compose ps
```

Open [CrowdRoom](http://127.0.0.1:8000). The app serves the API and frontend from the same origin. The stack contains the app and PostgreSQL 16; named volumes `pgdata` and `avatars` persist database records and uploaded avatars. Preserve both volumes when recreating containers. `docker compose down -v` deletes them.

The normal container entrypoint creates missing tables, but it does **not** alter existing tables or run Alembic upgrades.

### Upgrade an existing database

Keep the same Compose project name, `.env`, and volumes. Back up the database before changing its schema. Build the new source, stop the app to prevent writes during migration, then back up PostgreSQL and migrate with the new image:

```bash
docker compose build app
docker compose stop app
# This backup contains private account and authorization data.
umask 077
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$HOME/crowdroom-before-upgrade.dump"
docker compose run --rm --no-deps --entrypoint alembic app current
docker compose run --rm --no-deps --entrypoint alembic app upgrade head
docker compose up -d app
docker compose ps
```

Store the dump outside the checkout and back up the avatars volume too. If migration fails, leave the app stopped until the error is resolved or the backup is restored. Restarting CrowdRoom does not stop Spotify itself; after an upgrade, check the current song and playback state before resuming room control.

If an older installation created tables without an Alembic version, inspect the real schema and establish the matching migration baseline before upgrading. Do not run `stamp head` against an unknown schema: it records a version without applying missing changes. The empty-database instructions above are not a repair procedure for an existing untracked schema.

## DSM / NAS deployment

Build this checkout on a Docker host supported by your NAS, or build and transfer an image for its CPU architecture. Keep the Compose project/volume names when moving, and restore the database, avatars, and original keys. The NAS needs no Spotify browser or sound hardware; keep the chosen playback device online separately.

Expose CrowdRoom through an HTTPS reverse proxy and change these `.env` values:

```env
BIND_IP=0.0.0.0
HOST_PORT=8000
FRONTEND_URL=https://music.example.com
PUBLIC_URL=https://music.example.com
SPOTIFY_REDIRECT_URI=https://music.example.com/api/v1/auth/spotify/callback
COOKIE_SECURE=True
# Replace with the actual trusted proxy peer address seen by the app container.
FORWARDED_ALLOW_IPS=192.0.2.10
```

Use your real domain and register the HTTPS callback in Spotify. A private LAN address over HTTP is not a loopback callback. Forward requests and WebSocket upgrades to the app's published port. Restrict direct access to that port so public clients reach it through the proxy.

Configure the proxy to replace client-supplied forwarding headers, and set `FORWARDED_ALLOW_IPS` to its actual peer address. Uvicorn then resolves the client IP used by source rate limits. If the proxy is not trusted, all users may appear to have the proxy's IP; trusting arbitrary clients lets them spoof it. `DOCS_TRUST_PROXY` is a separate setting for the API documentation gate and can remain `False`.

A Synology reverse proxy or firewall change is deployment-specific; the example IP above is a placeholder, not a NAS default. Verify remote login, room updates, and the Spotify callback after setting up the proxy.

## Configuration reference

| Template | Use |
|----------|-----|
| `.env.example` | Full Docker stack, including local and DSM HTTPS settings |
| `backend/.env.dev.example` | Native backend development; copy to `backend/.env` |
| `backend/.env.prod.example` | Native HTTPS production backend; copy to `backend/.env` |

Compose passes the root `.env` into the app container. Native backend settings load `backend/.env`.

| Variable | Purpose |
|----------|---------|
| `SECRET_KEY` | Stable JWT signing key, including guest identity sessions |
| `ENCRYPTION_KEY` | Stable Fernet key for encrypted Spotify credentials |
| `AUTH_MODE` | `ONLINE` registered login or `LOCAL` local login; both offer guest entry |
| `DATABASE_URL` | Database DSN; the bundled Compose service hostname is `db` |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Initial bundled PostgreSQL credentials; match the DSN |
| `FRONTEND_URL` | Browser-facing frontend origin for CORS and redirects |
| `PUBLIC_URL` | Optional QR and copied-invite origin; defaults to `FRONTEND_URL` and does not change OAuth redirects |
| `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` | Spotify developer app credentials |
| `SPOTIFY_REDIRECT_URI` | Exact registered callback URL |
| `COOKIE_SECURE` | `True` for browser-facing HTTPS, `False` for local HTTP |
| `BIND_IP`, `HOST_PORT` | Published app interface and host port; internal port stays `8000` |
| `FORWARDED_ALLOW_IPS` | Trusted reverse-proxy peers for Uvicorn client IP resolution |
| `DEVICE_AUTH_TOKEN` | Optional shared secret for a compatible LAN QR display device |
| `DOCS_ALLOWED_HOSTS`, `DOCS_TRUST_PROXY` | IP allowlist and proxy handling for `/docs`, `/redoc`, and `/openapi.json` |

PostgreSQL initialization variables do not change the credentials of a database that already exists. Update existing database credentials deliberately instead of only editing `.env`.

## Development and testing

The backend uses Python 3.14+, FastAPI, SQLModel, Alembic, and Argon2 password hashing. The frontend uses Vue 3, Vue Router, Vite, Vitest, and pnpm. Spotify is the implemented music provider; provider adapters live in `backend/adapters/`.

For native development, copy `backend/.env.dev.example` to `backend/.env`, generate the required keys, and start the backend and Vite using the same browser host:

```bash
uv sync --dev
uv run alembic upgrade head
uv run uvicorn backend.main:app --host 127.0.0.1 --reload
# In another terminal:
pnpm -C frontend install --frozen-lockfile
pnpm -C frontend dev --host 127.0.0.1
```

Open [the Vite frontend](http://127.0.0.1:5173). Use [the backend docs](http://127.0.0.1:8000/docs) from an allowed client IP. A fresh LOCAL database defaults to SQLite; set `DATABASE_URL` explicitly to use PostgreSQL.

Run tests in Docker. With the app image built, the backend command uses a disposable SQLite database:

```bash
docker compose run --rm --no-deps --entrypoint sh -e DATABASE_URL=sqlite:// -e SPOTIFY_CLIENT_ID= -e SPOTIFY_CLIENT_SECRET= app -c 'uv sync --frozen --dev --no-install-project && .venv/bin/pytest -q backend/tests'
```

PostgreSQL concurrency tests additionally need `CROWDROOM_TEST_POSTGRES_URL` pointing to a dedicated test database. Never use the live room database as a test target.

For frontend tests and the production build:

```bash
docker run --rm -v "$PWD/frontend:/work" -w /work node:22-bookworm-slim sh -c 'corepack enable && corepack prepare pnpm@9 --activate && pnpm install --frozen-lockfile && pnpm test && pnpm build'
```

### Diagnose Spotify request volume

Completed Spotify HTTP requests are logged as `spotify_request` JSON events. Each event records the task source, normalized endpoint, HTTP status, duration, hashed app scope, and any supported 429 reason and retry deadline. Tokens, search text, user identifiers, track IDs, and response bodies are excluded. Calls blocked by the existing local cooldown do not count as outbound requests.

Summarize the captured container logs without contacting Spotify:

```bash
docker logs --timestamps crowdroom-app 2>&1 | docker exec -i crowdroom-app python scripts/spotify_usage_report.py
```

The report separates Accounts token requests from Web API requests and shows per-source, per-endpoint counts and the peak rolling 30-second Web API request count. `poll` includes all requests caused by a background task, including automatic track changes; use its endpoint breakdown to isolate state queries. Counts aggregate the captured sample across app scopes and are not Spotify's unpublished quota threshold. Docker container recreation removes its old logs, so export the report and logs before redeploying when investigating an incident.

A `QUOTA_EXCEEDED` reason identifies the developer quota response. An unspecified reason cannot prove which Spotify limit was reached. These diagnostics begin at deployment and cannot reconstruct requests or error bodies that were never recorded. Measure search traffic, background state reads, and playback commands separately before changing song-request quotas.

## Project layout

```text
backend/
  api/            # HTTP routes, auth dependencies, WebSocket endpoints
  adapters/       # Spotify and external metadata integrations
  core/           # Settings, security, rate limits, shared utilities
  db/             # SQLModel entities and database setup
  migrations/     # Alembic revisions
  repositories/   # Persistence
  schemas/        # Request/response contracts
  services/       # Queue, room, auth, playback business logic
  tests/          # Backend tests
frontend/
  src/            # Vue application and component tests
  public/         # Static assets
docker/
  entrypoint.sh   # Database readiness and runtime startup
```

An optional [ESP32 QR display](https://github.com/jaherhum/esp32-crowdroom) can show a room's invite QR code using `DEVICE_AUTH_TOKEN`.

## Contributing

This customized branch is `kyler/dev`. Use English [Conventional Commits](https://www.conventionalcommits.org/) by completed module, for example `feat(auth): add persistent guest sessions` or `docs: describe room access and deployment`.

## License

[GNU Affero General Public License v3.0](LICENSE).

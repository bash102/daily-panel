# Pi ↔ cloud sync protocol (M8)

The sync protocol between the box (`zozobox/sync/`) and the Cloudflare Worker (`cloud/worker/`). This file describes what both sides actually implement. The dashboard (M9) and anything else that talks to the cloud must follow it.

## Transport and auth

- **Base URL:** `ZOZOBOX_CLOUD_URL` on the Pi. It is the worker's `PUBLIC_BASE_URL`, for example `https://zozobox.example.workers.dev`.
- **Auth:** every `/api/*` request carries `Authorization: Bearer <BOX_TOKEN>`. On the Pi the token comes from `ZOZOBOX_BOX_TOKEN`. The worker compares it in constant time. A missing or wrong token gets `401 {"error":"unauthorized"}`.
- **Timestamps:** ISO 8601 with an explicit zone.
  - The Pi sends UTC with milliseconds, e.g. `2026-11-25T15:00:00.123Z` (`zozobox.sync.iso_utc`).
  - The worker accepts any offset and any number of fractional digits. It stores and returns `Date.toISOString()` form (UTC, milliseconds, `Z`).
- **Errors:** JSON `{"error": "..."}` with a status code:
  - 400: bad key or query
  - 401: auth
  - 404: no such object or route
  - 405: wrong method
  - 413: body over 50 MB
  - 422: invalid control command

## Keys

Archive keys are media-relative paths, the same strings the Pi's `Store` uses under `media/`. Examples:
- `recordings/zozo/2026/11/25/101500123-ab12cd34.wav`
- `prints/2026/11/25/101500123-letter.png`
- `content/story/2026/11/25/<id>/...`
- `content/index/<id>.json`
- `mood/...`

A valid key:
- is 1 to 512 characters from `[A-Za-z0-9._~+=@,-/]`
- does not start or end with `/`
- has no empty, `.` or `..` segments, and no backslashes

| Prefix | PUT (Pi writes) | GET (Pi reads) |
|---|---|---|
| `recordings/zozo/`, `prints/`, `mood/`, `content/`, `phrases/`, `events/`, `logs/`, `control/` | yes | yes |
| `recordings/grandma/` | **no** (the worker writes these) | yes |

In the URL path the key is percent-encoded segment by segment (slashes kept), and the worker decodes it. An encoded `..` is still rejected.

## Endpoints

### `PUT /api/objects/<key>`
The body is the raw bytes, and `Content-Type` is stored with the object. The call is idempotent: a repeated PUT overwrites the object with the same bytes.

Response `200 {"ok": true, "key": "...", "bytes": N}`.

For keys under `control/` the body must be JSON. It is validated first: an invalid command gets a 422 and is not stored. It is then stored in R2 like any other object **and** applied. The response is `200 {"ok": true, "key": "...", "applied": true|false, "result": "..."}`.

### `GET /api/objects/<key>`
The object's bytes with their stored `Content-Type`. `404` if it is missing.

### `GET /api/grandma/messages?since=<ISO>&limit=<n>`
Grandma's archived messages with `recorded_at >= since`. The bound is **inclusive**. With no `since`, it returns all of them.

The response is ordered oldest first (newest last), up to `limit` messages: the default is 50 and the maximum 200. The id is Twilio's RecordingSid.

```json
[{"id": "RE…", "key": "recordings/grandma/2026/11/25/RE….wav", "recorded_at": "2026-11-25T14:59:45.000Z", "duration_s": 15}]
```

The audio is 8 kHz WAV as Twilio recorded it. Download it with `GET /api/objects/<key>`.

**Archive format (deviation from SPEC 7.3, "Audio is stored as Opus").** The archive keeps voice audio as WAV: grandma's as Twilio's 8 kHz original (`audio/wav`), Zozo's as the engine saves it. Reasons: the worker cannot transcode (no ffmpeg in Workers), Twilio's `<Play>` needs a format it plays (WAV/MP3, not Opus), the engine writes WAV for Zozo too, and the messages are short (about 16 KB per second at 8 kHz), so storage cost is negligible. Turning this into Opus would be a project-wide change: an Opus copy made on the Pi and uploaded under a new prefix, plus MP3/WAV for playback on the phone. The SPEC owner should either accept WAV or schedule that change.

### `GET /api/zozo/messages`
The worker's view of Zozo's replies, at most the last 50:

```json
[{"rec_id", "status", "recorded_at", "publish_at", "published_at", "withheld_at", "sms_sent_at"}]
```

`status` is one of `pending`, `published`, `withheld` or `superseded`.

### `GET /api/config`, `GET /api/health`
- `/api/config` returns the config the worker is using (the defaults until the Pi pushes one).
- `/api/health` returns `{"ok": true, "now": "<ISO>"}`.

## Control commands (`PUT /api/objects/control/...`)

| Key | Body | Effect |
|---|---|---|
| `control/config.json` | `{"quiet_hours": {"start": "HH:MM", "end": "HH:MM"}, "timezone": "<IANA>", "review_delay_s": <int 0..604800>}` | Replaces the worker's config. Defaults until the first push: 19:00–07:00, `America/New_York`, 3600. |
| `control/zozo/<rec_id>.json` | `{"action": "new", "rec_id", "media_key", "recorded_at", "duration_s"}` | Registers a pending Zozo message. `rec_id` must match the key and be `[A-Za-z0-9_]{1,100}` (the store uses uuid hex). `media_key` must be under `recordings/zozo/`. `duration_s` is a number or null. |
| `control/zozo/<rec_id>-withhold.json` | `{"action": "withhold", "rec_id"}` | Never publish it. If it is already published, unpublish it. Final. |
| any other `control/...` | any JSON | Stored, not applied (`applied: false`). |

### Registering a message ("new")
- Repeating a "new" is a no-op (`result: "exists"`).
- If a withhold for that id arrived first, the message stays withheld (`result: "withheld"`).
- **Single live slot:** a newer message marks older *pending* ones `superseded`. A message recorded before an existing pending or published one is registered as `superseded` straight away. Superseded messages are never published.

### Review and publish (worker cron, every 5 minutes)
- `publish_at` = `recorded_at` + `review_delay_s` of review clock. The review clock only runs **outside quiet hours**, which are evaluated in `timezone` and may span midnight (start inclusive, end exclusive; start = end means never quiet).
  - A message recorded during quiet hours starts its clock when they end.
  - A window that runs out exactly at the start of quiet hours publishes when they end.
- `publish_at` is recomputed on every run, so a config change applies to messages still pending.
- A message publishes when all of these hold:
  - `now >= publish_at`
  - it is not quiet hours now
  - its `media_key` object exists in R2. The "new" command may arrive before the audio; the message then waits for it.
- On publishing, the worker texts `GRANDMA_NUMBER` "New message from Zozo! Call <TWILIO_FROM_NUMBER> to listen." once per published message (retried up to 5 times on failure).
- Grandma's "press 1" plays the newest (by `recorded_at`) published, non-withheld message. After a withhold, she hears the previous published one.

## What the Pi sync client does (`zozobox/sync/`)

`SyncClient(store, base_url, token, clock=None, *, transport=None, normalizer="auto", ...)`. `run_once()` makes one pass and returns a `SyncReport`. `run(stop)` loops: a pass every 30 s. While offline or failing, the interval doubles up to 600 s (the exponent is capped, so the loop runs forever).

The Store must be opened with `recover_outbox=True` in this process, and only in this process.

Each pass:

1. **Config.** `{quiet_hours, timezone, review_delay_s}` from `store.settings`. When it differs from the config last queued, the client queues `control/config.json` in the outbox. The first pass always queues it.
2. **Announce.** Each new **live** recording in the Zozo slot gets a `control/zozo/<rec_id>.json` "new" command queued in the outbox:
   - `media_key` is the recording's media path, which the engine already queued for upload.
   - `recorded_at` is the recording's `created_at`.
   - Superseded or withheld recordings are not announced.
   - Each id is announced once (tracked in `sync_state.json`).
3. **Upload.**
   - **Withholds first.** Every pending `control/zozo/<id>-withhold.json` is sent before anything else, ignoring its backoff. A withhold must reach the cloud before the review window can end (SPEC 2.2.4), even right after a long outage.
   - **Stale "new" commands are dropped.** Just before a `control/zozo/<id>.json` "new" command is sent, the client looks the recording up. If it was withheld or purged since the command was queued, the item is closed out without sending, so the cloud never learns about it.
   - Then the client claims outbox batches:
   - File items are sent with `PUT` as raw bytes, with the item's `content_type`, or a type guessed from the file name.
   - JSON records are sent with `PUT` as JSON (`application/json`).
   - A 2xx answer means `mark_done`. Anything else means `mark_failed`: the item is retried with the outbox's exponential backoff, forever.
   - When the box is offline, only the item that was actually tried is marked failed. The rest of the batch goes back to the queue, due now, with its attempt count unchanged, and the pass skips its remaining network steps.
   - A 401/403 stops the batch the same way.
   - A file that no longer exists locally is closed out with a warning, because it can never upload.
4. **Grandma.** `GET /api/grandma/messages?since=<watermark>`. A message is skipped when its id was imported before. The client checks, in order: the `grandma_imported` ids in `sync_state.json`, the store, and the `recording` events with `source: "cloud"` in the store's event log. Events survive a purge, so a message the parent purged never comes back, even when `sync_state.json` is lost. Each new message is handled like this:
   - The client downloads it and loudness-normalizes it to -16 LUFS, 48 kHz mono WAV (`zozobox.audio.loudness.normalize`, when ffmpeg is present). If normalizing fails, it keeps the original audio.
   - It writes the audio to `media/<key>`, i.e. `media/recordings/grandma/YYYY/MM/DD/<id>.wav`.
   - It calls `store.recordings.put(Slot.GRANDMA, key, rec_id=id, created_at=recorded_at, duration_s=..., meta={"source": "cloud", "key": key})`. The new message is live and supersedes the previous one, and the grandma LED shows it as unheard. A message older than the live one is archived as superseded.
   - It logs a `recording` event.
   - The watermark advances per message, so a failed download is retried next pass.
   - Offline, 408, 429 and 5xx errors are retried forever. A failure that retrying cannot fix (another 4xx such as a 404 for a missing object, a key outside `recordings/grandma/`, an empty body) is retried for 3 passes. After that the message is recorded in `grandma_skipped`, reported once as an error, and the watermark moves past it, so newer messages still arrive.
5. **Status.** `GET /api/zozo/messages`. For each `published` reply, the client calls `store.recordings.mark_published(rec_id, at=published_at)`.

`zozobox.sync.load_status(data_dir)` returns the last pass's summary for the dashboard: `last_run_at`, `last_ok_at`, `online`, `last_error` and totals.

### Dashboard (M9) obligations

- **Withholding a Zozo reply.** Do both:
  1. call `store.recordings.withhold(rec_id)`
  2. queue the command in the outbox: `zozobox.sync.enqueue_withhold(store, rec_id)`, which is `store.outbox.enqueue("control/zozo/<rec_id>-withhold.json", record={"action": "withhold", "rec_id": rec_id}, content_type="application/json")`
- Never call the worker directly. The sync client uploads the command.
- Deleting a grandma message is local only (`store.recordings.withhold`). The archive keeps it.
- Settings changes need no action. The next pass notices them and pushes `control/config.json`.

## Grandma's line: PIN lockouts

- SPEC 7.2: 3 wrong PINs from one number within an hour lock that number out for an hour, and the parent gets one text.
- Beyond the SPEC, a line-wide limit guards against guessing from many numbers: `GLOBAL_PIN_FAILURE_LIMIT` (default 10) wrong PINs from anyone in an hour lock the line for an hour and text the parent.
- The line lock **never applies to `GRANDMA_NUMBER`** (compared by digits, with or without +1). Strangers therefore cannot lock grandma out of her own line. Her number still has its own 3-try lock.
- Trade-off: caller ID can be spoofed. A caller spoofing grandma's number gets only that number's 3 tries per hour. They can also lock that number for an hour, which SPEC 7.2's per-number rule allows anyway.
- `GLOBAL_PIN_FAILURE_LIMIT=0` turns the line-wide lock off.

## Running it

```bash
export ZOZOBOX_CLOUD_URL=https://zozobox.example.workers.dev
export ZOZOBOX_BOX_TOKEN=...            # = the worker's BOX_TOKEN secret
export ZOZOBOX_DATA_DIR=/var/lib/zozobox
python -m zozobox.sync once -v          # one pass; exit 0 ok, 1 config/store error, 2 errors (still queued)
python -m zozobox.sync run              # loop until SIGTERM/SIGINT (--interval, --max-backoff)
```

# zozobox worker

Cloudflare Worker for Zozo's button box (SPEC 7.0, 7.2, 7.3; PLAN M8):

- **Grandma's phone line**: a Twilio voice webhook. A 4-digit PIN, then press 1 to hear Zozo's latest published message, or press 2 to record a message for him (up to 120 s).
- **Review timer**: Zozo's replies publish `review_delay_s` (default 1 hour) after recording. The review clock pauses during quiet hours. A cron every 5 minutes publishes due messages and texts grandma once for each one.
- **Archive**: R2 bucket `ARCHIVE` is the primary copy. Grandma's recordings are copied from Twilio into R2, then deleted from Twilio.
- **Pi sync API**: the box uploads its outbox and downloads grandma's messages. The protocol is in [docs/SYNC.md](../../docs/SYNC.md).

State lives in D1 (`DB`). The schema is in `migrations/`.

## Layout

| File | What it does |
|---|---|
| `src/index.ts` | Worker entry (`fetch`, `scheduled`). Maps the bindings to `Deps`. |
| `src/app.ts` | Router. Checks the Twilio signature on `/twilio/*` and serves `/media/<token>` and `/api/*`. |
| `src/ivr.ts` | TwiML for the call flow, plus recording ingest (Twilio to R2 and D1, then deleting Twilio's copy). |
| `src/pin.ts` | PIN check, per-caller lockout and the line-wide brute-force limit. |
| `src/api.ts`, `src/control.ts`, `src/keys.ts` | Pi API, the `control/` commands and key validation. |
| `src/cron.ts` | Publishing, grandma's texts and retries. |
| `src/time.ts` | Quiet hours in an IANA timezone and `computePublishAt` (review clock with the quiet-hours pause). |
| `src/twilioSignature.ts`, `src/mediaToken.ts`, `src/crypto.ts` | HMAC-SHA1 request validation, signed media URLs and a constant-time compare (WebCrypto). |
| `src/twilioApi.ts` | Twilio REST calls (SMS, download recording, delete recording) over an injected `fetch`. |
| `src/repo.ts` | All the SQL. |
| `test/` | vitest. D1 is faked with `node:sqlite` running the real migrations, and R2 with a Map. Twilio is a fake, or a mocked `fetch`. No network. |

## Develop

```bash
npm install
npm test            # vitest
npm run typecheck   # tsc --noEmit (worker types + test types)
npx wrangler dev    # local; put secrets in .dev.vars (git-ignored)
```

Needs Node 22 or later. The tests use `node:sqlite`, which may print an ExperimentalWarning.

## Deploy

1. Log in: `npx wrangler login`.
2. Create the D1 database: `npx wrangler d1 create zozobox`. Copy the `database_id` it prints into `wrangler.toml`.
3. Apply the migrations: `npx wrangler d1 migrations apply zozobox --remote`.
4. Create the R2 bucket: `npx wrangler r2 bucket create zozobox-archive`.
5. Set `PUBLIC_BASE_URL` in `wrangler.toml` to the worker's public https origin, for example `https://zozobox.<account>.workers.dev` or a custom domain. Use no trailing path. Twilio's webhook URLs must start with exactly this origin.
6. Set the secrets. Each command prompts for the value, so it never goes into a file or your shell history.
   ```bash
   for s in TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN TWILIO_FROM_NUMBER GRANDMA_NUMBER \
            PARENT_NUMBER GRANDMA_PIN BOX_TOKEN MEDIA_SIGNING_KEY; do
     npx wrangler secret put "$s"
   done
   ```
   - Phone numbers are in E.164 form, for example `+15551234567`.
   - `TWILIO_FROM_NUMBER` is the Twilio number grandma calls.
   - `GRANDMA_PIN` is exactly 4 digits.
   - `BOX_TOKEN` and `MEDIA_SIGNING_KEY` are long random strings, for example from `openssl rand -base64 32`. They must be different from each other. Put the same `BOX_TOKEN` on the Pi as `ZOZOBOX_BOX_TOKEN`.
7. Deploy: `npx wrangler deploy`. The cron trigger (`*/5 * * * *`) is deployed with the worker.
8. In the Twilio console, under Phone Numbers, open the number and set:
   - **A call comes in**: Webhook, `POST https://<PUBLIC_BASE_URL host>/twilio/voice`.
   - Leave the SMS webhook empty. The worker only sends texts.
   - The other URLs (`/twilio/pin`, `/twilio/menu`, `/twilio/recorded`, `/twilio/recording-status`) are set by the TwiML itself.
9. On the Pi, set `ZOZOBOX_CLOUD_URL=<PUBLIC_BASE_URL>` and `ZOZOBOX_BOX_TOKEN`, then run `python -m zozobox.sync run`.
10. Smoke test: `curl -H "Authorization: Bearer $BOX_TOKEN" $PUBLIC_BASE_URL/api/health`. Then call the number.

Optional vars:
- `TWILIO_VOICE`: the `<Say>` voice. The default is `Polly.Joanna`.
- `GLOBAL_PIN_FAILURE_LIMIT`: wrong PINs from all callers in one hour before the line locks for an hour, for every number except `GRANDMA_NUMBER`. The default is 10, and `0` turns the line lock off. See docs/SYNC.md, "Grandma's line: PIN lockouts".

## Behavior notes

**Security**
- Every `/twilio/*` request must carry a valid `X-Twilio-Signature`: HMAC-SHA1 with the auth token, over the full URL plus the sorted POST params. Anything else gets a 403.
- The signature is checked against the URL the worker received, and against the same path and query under `PUBLIC_BASE_URL`.
- The menu, the recording and the recording callback also require a call SID that entered the right PIN within the last 2 hours.
- A recording from a call that did not enter the PIN is deleted from Twilio and not archived.

**PIN lockout**
- Three wrong PINs from one caller within an hour hang up the call and lock that caller out for 1 hour. `PARENT_NUMBER` gets one text per lockout.
- The line-wide limit locks the line for everyone, grandma included, and also sends one text.
- Callers with a hidden number all share one "caller" key.

**Playback**
- Grandma hears the newest published message that has not been withheld.
- `<Play>` uses `/media/<token>`. The token is HMAC-SHA256 signed and expires after 10 minutes, and only keys under `recordings/` can be played.

**Publishing**
- The review clock runs only outside quiet hours. Quiet hours are local wall-clock times in the configured timezone and may span midnight.
- A message recorded during quiet hours starts its clock when they end. A window that runs out exactly when quiet hours start waits until they end.
- Nothing publishes while it is quiet.
- A message whose audio has not reached R2 yet waits for it.
- A newer Zozo reply supersedes older pending ones, so only one is published (the single live slot, SPEC 2.2). A withhold is final.
- Grandma's text is retried up to 5 times.

**Ingest**
- On `recordingStatusCallback`, the recording is downloaded as WAV with Basic auth. Downloads are only made from `api.twilio.com`, so the credentials never go anywhere else.
- It is stored at `recordings/grandma/YYYY/MM/DD/<RecordingSid>.wav`, using the box's local date, and registered in D1. Only then is Twilio's copy deleted.
- Failures are retried by the cron, up to 12 attempts.

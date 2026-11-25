# Deploying Zozo's box

The box runs on its Raspberry Pi under **pi-ops** (`bash102/pi-ops`), the owner's Pi deployment system. pi-ops' `git-deploy` clones this repo onto the Pi, tracks GitHub `main`, and runs it as one systemd unit. **Only merges to `main` deploy**: a push to `main` reaches the Pi through the GitHub webhook (or pi-ops' 10-minute timer), and `git-deploy` pulls, reinstalls when `pyproject.toml` or `deploy/pi.toml` changed, restarts the unit and checks its health. Branches never run on the box.

The hardware checks that need a person standing at the box are in [BRINGUP.md](BRINGUP.md). The Cloudflare Worker is deployed separately, as before ([cloud/worker/README.md](../cloud/worker/README.md)).

## What runs where

Everything runs as the pi-ops **service user, `bash102`** (pi-ops hardcodes that name, see step 1). Nothing the box writes at runtime is inside the clone.

| Path | What |
|---|---|
| `~/apps/daily-panel` | The clone that `git-deploy` manages, on `main`. Never edit it by hand: a local change blocks deploys. |
| `~/apps/daily-panel/.venv` | The virtualenv. It sees the system site packages, so apt's `python3-lgpio` (gpiozero's pin factory) is importable. |
| `~/.config/zozobox/box.toml` | Static box config: pins, cards, ceilings, printer. Template: `deploy/box.toml.example`. |
| `~/.config/zozobox/env` | Secrets: the pi-ops `env_file`, mode `600`. Template: `deploy/env.example`. |
| `~/.local/share/zozobox` | All data: the SQLite store, media, soundboard, chimes, locks, the setup token, `supervisor-state.json`. |
| `app-zozobox.service` | The one unit, rendered by `git-deploy` from [`deploy/pi.toml`](../deploy/pi.toml). Logs: `journalctl -u app-zozobox`. |
| `https://<pi>.<tailnet>.ts.net:8792/` | The parent dashboard, over Tailscale only (`tailscale serve`, set up by `git-deploy`). |

The unit runs `python -m zozobox.run`, a small supervisor ([`zozobox/run.py`](../zozobox/run.py)) that starts everything else as child processes. Each child's log lines are prefixed with its name (`engine: ...`, `dashboard: ...`).

| Child | Command | When |
|---|---|---|
| `engine` | `python -m zozobox.app` | Always. Restarted with backoff (1 s up to 60 s) if it exits. |
| `dashboard` | `python -m zozobox.dashboard --host 127.0.0.1 --port 8791 --printer brother --secure-cookies` | Always. Its `/healthz` is pi-ops' health check. |
| `sync` | `python -m zozobox.sync run` | When `ZOZOBOX_CLOUD_URL` and `ZOZOBOX_BOX_TOKEN` are filled in (https only). |
| `nightly` | `python -m zozobox.content nightly` | Daily at about 02:30 box-local time. If the Pi was off then, or the run was cut short, it runs at the next start. |
| `mood` | `python -m zozobox.mood reprocess` | Hourly: mood takes recorded while offline. |
| `bootstrap` | `python -m zozobox.content bootstrap` | Automatically while fixed phrases are missing (not just awaiting approval), at most once every 12 hours. |

`nightly`, `mood` and `bootstrap` need both AI keys. These three heavy jobs never run at the same time (the supervisor waits, and content also holds its own lock), and the content CLI enforces the spend cap. A child that exits with its configuration-error status (engine or dashboard `2`, a bad `box.toml` or a store that cannot be opened; sync `3`) is not restarted in a loop: the supervisor logs the error and retries every 5 minutes. Any other exit, an uncaught exception (`1`) included, is restarted with backoff. Anything missing from the env file is logged once at start-up and only the parts that need it are skipped, so the engine and the dashboard keep running and the health check stays green.

Limits the supervisor puts on its jobs, because the unit is one cgroup:

- **Memory.** pi-ops' unit has `MemoryMax=600M` and no `OOMPolicy=`, so systemd's default `OOMPolicy=stop` applies: if the kernel OOM-kills **any** process in the unit, systemd stops the whole box (engine, dashboard, sync) and `Restart=on-failure` starts it again 5 s later. `pi.toml` cannot change that. So each heavy job runs with `RLIMIT_DATA` = `ZOZOBOX_JOB_MEMORY_MB` (default 384 MB, set it in the env file; `0` turns it off) and fails with a `MemoryError` inside the job rather than tripping the cgroup.
- **No restart loop.** Each heavy job's start is written to `supervisor-state.json` before it runs. One interrupted run (a deploy restart, an OOM stop) is retried at the next start; a job started twice without ever finishing is not started again for that nightly slot (until the next 02:30), within 12 h for bootstrap, or within the hour for mood, and the journal says `started 2 times without finishing`.
- **Deadlines.** nightly and bootstrap 2 h, mood 30 min, `content status` 5 min. Past it the job's process group gets SIGTERM, then SIGKILL 25 s later, and an error is logged.
- **Priority.** Heavy jobs run at nice 10 and, with `ionice`, IO class idle.
- **Clock.** Nightly, catch-up and bootstrap decisions wait until the clock is NTP-synchronized (the Pi 3 has no RTC); the journal says so while it waits. `ZOZOBOX_REQUIRE_TIME_SYNC=0` turns that off.
- **Code on disk.** `git-deploy` rebases the clone **before** it installs and restarts the unit. While that install runs, or when it fails (`install-failed`) or the health check fails (`unhealthy`) and the app is blocked, the old supervisor is still running on the new files. It notices (the clone's git HEAD changed since it started), starts **no new jobs** and logs `code changed on disk since start`; crashed children are still restarted so the box keeps working (they import the new code). Fix the cause, then `git-deploy unblock daily-panel` and `git-deploy sync daily-panel`, which restarts the unit.

## 1. Which Pi

The box needs **the Pi it is physically wired to**: the Raspberry Pi 3 with the buttons, the LED driver, the handset, the speaker and the printer. pi-ops' README describes the Pi `raspberryfly` (Debian 13 arm64, service user `bash102`, webhook at `https://raspberryfly.tailaba449.ts.net/hooks/sync`). If the box's Pi is not `raspberryfly`, **bootstrap pi-ops on the box's Pi first**, following pi-ops' README and `bootstrap.sh`: Tailscale up with Funnel for `/hooks`, `sudo sh bootstrap.sh` from a pi-ops checkout, and `/etc/git-webhook/env`. Every `raspberryfly` URL below then becomes that Pi's tailnet name.

**The service user must be `bash102`.** pi-ops hardcodes it: `bootstrap.sh` creates `/etc/git-webhook` with group `bash102` (it fails without that group), its `git-mirror-sync`, `git-deploy-commit` and `git-webhook` units run `User=bash102`, and `git-deploy` (running as that user) renders the app unit's `User=` from whoever runs it. So create `bash102` on the box's Pi, with passwordless sudo for `systemctl` and `tailscale` (git-deploy runs `sudo systemctl daemon-reload/enable/restart` and `sudo tailscale serve` from the webhook and the timers, without a terminal). Another user name would need changes in pi-ops, which this project does not make. `host-setup.sh` warns if you pass any other `--user`.

On a 1 GB Pi 3 the box alone is capped at `memory_max = "600M"`. That figure is an **estimate, not a measurement** (see [`deploy/pi.toml`](../deploy/pi.toml)). Because one OOM kill stops the whole unit (above), measure it during bring-up: after a nightly run and a mood run, `systemctl show -p MemoryPeak app-zozobox` (and `-p MemoryCurrent`) give the unit's peak. Keep `memory_max` at least 25% above the peak (it is a `deploy/pi.toml` change, merged to `main`), and keep `ZOZOBOX_JOB_MEMORY_MB` below `memory_max` minus what the engine, dashboard and sync use together. If you do put the box on a Pi that already runs other pi-ops apps (`raspberryfly` runs dsdigest with a 700M cap and emporia-data with 300M), their caps add up to more than the RAM; a dedicated Pi for the box is strongly preferred.

Before you start:

- **OS:** Raspberry Pi OS Lite (64-bit) or Debian 13 arm64. Raspberry Pi OS is easier: it has `python3-lgpio`, `raspi-config` and the GPIO and I2C groups. On plain Debian, `host-setup.sh` creates what is missing (below).
- **Time zone:** set the Pi's time zone to the box's (`sudo timedatectl set-timezone America/New_York`). The nightly run uses it (or set `ZOZOBOX_TZ` in the env file). Quiet hours, the box day and the grandma review window use the box's own **Time zone setting on the dashboard** (step 8), which you set to the same zone.
- **Clock:** `timedatectl` should show `System clock synchronized: yes`. The Pi 3 has no RTC. Plain Debian has no `fake-hwclock`; `systemd-timesyncd` saves the time in `/var/lib/systemd/timesync/clock` and restores it at boot, so after a power cut the clock starts at the last saved time until NTP syncs (Raspberry Pi OS also has `fake-hwclock`). Make sure an NTP client runs (`systemd-timesyncd` or `chrony`): the supervisor holds the nightly run, its catch-up and the bootstrap until the clock is synchronized.
- **Ports:** the box uses `127.0.0.1:8791` and serves the dashboard on tailnet port `8792`. On the pi-ops Pi, `8000`/`8443` (dsdigest) and `8787`/`8788` (emporia-data) are already taken; `443` is the public Funnel for `/hooks` and is never used for apps.

## 2. Deploy key and ssh alias

As in pi-ops' "Add an app", step 2. On the Pi, as the service user:

```bash
ssh-keygen -t ed25519 -N '' -f ~/.ssh/gh_deploy_rw_daily-panel -C "$(hostname) daily-panel"
cat >> ~/.ssh/config <<'EOF'
Host github-rw-daily-panel
  HostName github.com
  User git
  IdentityFile ~/.ssh/gh_deploy_rw_daily-panel
  IdentitiesOnly yes
EOF
cat ~/.ssh/gh_deploy_rw_daily-panel.pub
```

Add the public key to the GitHub repo under **Settings → Deploy keys**, **read-only** (do not tick "Allow write access"): `deploy/pi.toml` has `autocommit = []`, so `git-deploy` never pushes. The alias must be lowercase (git-lfs lowercases host names). Check it: `ssh -T github-rw-daily-panel` greets you with the repo name.

## 3. host-setup.sh (once, as root)

`git-deploy` installs and runs the app but cannot do root setup. [`deploy/host-setup.sh`](../deploy/host-setup.sh) does that part, once (it is idempotent, so re-running it is safe). The clone does not exist yet, so run it from a temporary one:

```bash
git clone -q github-rw-daily-panel:bash102/daily-panel.git /tmp/zozobox-setup
sudo /tmp/zozobox-setup/deploy/host-setup.sh          # as bash102; as anyone else: --user bash102
rm -rf /tmp/zozobox-setup
```

Later re-runs: `sudo ~/apps/daily-panel/deploy/host-setup.sh`. It does the following:

- **apt:** `python3-venv python3-dev python3-lgpio alsa-utils ffmpeg libusb-1.0-0 fonts-dejavu-core i2c-tools`. A package that this distribution lacks is skipped with a warning. Without `python3-lgpio` (plain Debian), pip builds `lgpio` from the `[pi]` extra, which needs a C compiler, `swig` and `liblgpio-dev`: the script installs `build-essential swig liblgpio-dev` when available. If `liblgpio-dev` is not packaged either, install `build-essential swig` and build the lgpio C library from source (github.com/joan2937/lg, `make && sudo make install`). If `git-deploy install` already ran and failed on it, the app is `blocked` (`install-failed`) and, on a first install, `app-zozobox.service` does not exist yet, so `systemctl restart` cannot help: run `git-deploy unblock daily-panel`, then `git-deploy sync daily-panel`. With no good deploy recorded yet, that sync runs the manifest's `install` again.
- **I2C** for the PCA9685: `raspi-config nonint do_i2c 0` when `raspi-config` exists. Otherwise it appends a marked block `[all]` / `dtoverlay=` / `dtparam=i2c_arm=on` to `/boot/firmware/config.txt` (or `/boot/config.txt`): the `[all]` and the empty `dtoverlay=` make sure the parameter applies to the base device tree on every model, not to an earlier overlay or a `[pi4]` section. On Debian's `raspi-firmware`, which regenerates `config.txt` on kernel upgrades, the block also goes into `/etc/default/raspi-firmware-custom`, which it appends. It loads the `i2c-dev` module. **Reboot** when it says so, then check that `/dev/i2c-1` exists (the script skips this step once it does).
- **Groups:** adds the service user (default `$SUDO_USER`, else `bash102`; `--user NAME` overrides, but only `bash102` works with pi-ops) to `gpio i2c audio lp plugdev`, those that exist. On plain Debian, which has no rules for them, it creates `gpio` and `i2c` and installs `deploy/udev/99-zozobox-gpio-i2c.rules` (`/dev/gpiochip*` and `/dev/i2c-*`, mode `0660`). The pi-ops unit has no `SupplementaryGroups=`; systemd gives it the user's groups at each start.
- **udev rule for the QL-800** (`04f9:209b`, group `lp`).
- **`/etc/asound.conf`** (the softvol hard caps), only if there is none. Otherwise it writes `/etc/asound.conf.zozobox-new` next to it.
- **sudoers:** `/etc/sudoers.d/zozobox-poweroff`, rendered from `deploy/sudoers/zozobox-poweroff` for the service user and checked with `visudo -cf`. The rule names exactly `/usr/bin/systemctl poweroff` (the parent chord), but under pi-ops it adds no confinement: `bash102` already has passwordless `sudo systemctl` for git-deploy (see "Privileges" below).
- **Clock:** warns if the clock is not NTP-synchronized.
- **`~/.config/zozobox`** (mode `700`) with `box.toml` from `deploy/box.toml.example` and `env` from `deploy/env.example` (mode `600`, owned by the service user), and **`~/.local/share/zozobox`** (mode `700`). Existing files are never overwritten.

## 4. Secrets and box.toml

Edit the env file as the service user: `nano ~/.config/zozobox/env`. Keep it mode `600`: pi-ops blocks the deploy if it is missing, inside the clone or readable by group or other. The format is systemd's `EnvironmentFile`: one `KEY=VALUE` per line, no `export`, single quotes around a value with spaces, `#`, `\` or a double quote (see pi-ops' README, "Secrets").

| Variable | Where it comes from |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic Console. Create a **separate key** in a "zozobox" workspace and set a workspace spend limit (SPEC 7.0). |
| `OPENAI_API_KEY` | OpenAI. Create a key in a separate project and set a project budget. |
| `ZOZOBOX_CLOUD_URL` | The worker's `PUBLIC_BASE_URL` (step 5), an `https://` URL. Sync is not started (and the token is never sent) while this or the token is still `replace-me`, or when the URL is not https. |
| `ZOZOBOX_BOX_TOKEN` | The same value as the worker's `BOX_TOKEN` secret. |

You can deploy before the worker exists: leave the cloud values as `replace-me` and fill them in after step 5. Editing the env file does not deploy anything; afterwards run `sudo systemctl restart app-zozobox`.

`~/.config/zozobox/box.toml` holds the wiring and devices. Every key is optional and the defaults are listed in the file. Once the app is installed (step 6), check an edit with:

```bash
~/apps/daily-panel/.venv/bin/python -m zozobox.app --check-config
sudo systemctl restart app-zozobox
```

A typo, such as an unknown key, a pin used twice or a role missing, makes the engine (and the dashboard, which reads the same file for reprints) exit with status 2 and a clear message in `journalctl -u app-zozobox`. The supervisor does not restart them in a loop on that status: it retries every 5 minutes, so fix the file and restart the unit. Note that the health check then fails, so a deploy at that moment reports `unhealthy` (see Operating). The values you will most likely change during bring-up:

- `[buttons].pins`: BCM pin numbers.
- `[leds].channels`: PCA9685 channels.
- `[leds].invert`: flip it if brightness comes out backwards.
- `[audio.handset|speaker|mic].card`: the ALSA short names from `aplay -l`. Change them in `/etc/asound.conf` too.
- `mixer_control`
- `max_fraction`: the volume ceilings.
- `[printer].identifier`

## 5. Cloudflare Worker and Twilio

The worker is deployed from any computer with Node 22 or later, not from the Pi, exactly as before. Follow [cloud/worker/README.md](../cloud/worker/README.md), "Deploy". In short:

1. Run `npx wrangler login`.
2. Create the D1 database and copy its id into `wrangler.toml`, then apply the migrations.
3. Create the R2 bucket `zozobox-archive`.
4. Set `PUBLIC_BASE_URL`.
5. Set the secrets: `TWILIO_*`, `GRANDMA_NUMBER`, `PARENT_NUMBER`, `GRANDMA_PIN`, `BOX_TOKEN` and `MEDIA_SIGNING_KEY`.
6. Run `npx wrangler deploy`.

Put the same `BOX_TOKEN` into `~/.config/zozobox/env` as `ZOZOBOX_BOX_TOKEN`, and the URL as `ZOZOBOX_CLOUD_URL`. Check that the Pi can reach the worker, then restart:

```bash
(set -a; . ~/.config/zozobox/env; curl -H "Authorization: Bearer $ZOZOBOX_BOX_TOKEN" "$ZOZOBOX_CLOUD_URL/api/health")
sudo systemctl restart app-zozobox && journalctl -u app-zozobox -g '^sync:' -n 20
```

Twilio:

1. Buy a US local voice-capable number in the Twilio console (about $1.15/month).
2. Under Phone Numbers, open the number. Set **A call comes in** to Webhook, `POST https://<worker host>/twilio/voice`. Leave the SMS webhook empty.
3. The texts (to grandma when Zozo's reply is published, and to the parent on a PIN lockout) are sent from this number. For US numbers, complete Twilio's A2P 10DLC registration, or texts may be filtered.
4. Test: call the number, enter the PIN, press 2 and leave a message. Within a sync pass (30 s) the Grandma LED should be bright. [BRINGUP.md](BRINGUP.md) has the full call test.

## 6. Install with git-deploy

On the Pi, as the service user:

```bash
git-deploy install daily-panel
git-deploy status daily-panel
```

`install` clones into `~/apps/daily-panel`, creates `.venv`, runs the manifest's `install` (re-running `python3 -m venv --system-site-packages .venv` over git-deploy's plain venv, which switches it to see the system packages and keeps what is installed, then `pip install -e '.[pi]'`), writes and enables `app-zozobox.service`, starts it, waits up to 30 s for `http://127.0.0.1:8791/healthz`, and then runs `sudo tailscale serve --bg --https=8792 http://127.0.0.1:8791`. The first `pip install` on a Pi 3 takes several minutes.

`git-deploy status daily-panel` should show the unit `active` and `env_file: /home/bash102/.config/zozobox/env (ok)`. If install comes back `blocked` (for example the env file is missing or too open), fix the cause, then `git-deploy unblock daily-panel` and `git-deploy sync daily-panel`. Re-running `install` does not clear a block.

## 7. GitHub webhook

So that merges deploy at once (the 10-minute timer catches up otherwise), add a webhook to the GitHub repo: payload URL `https://raspberryfly.tailaba449.ts.net/hooks/sync` (the Pi's Funnel URL, if the box's Pi is a different one), **content type `application/json`**, the secret from `/etc/git-webhook/env`, **push events only**. Only pushes to `main` trigger a sync.

## 8. First run: password, bootstrap, approvals

**Dashboard password.** Until a parent password exists, `/setup` only works with a one-time token:

```bash
cat ~/.local/share/zozobox/dashboard-setup-token
journalctl -u app-zozobox -g setup        # the same token, in the dashboard's log
```

From a tailnet device, open `https://<pi>.<tailnet>.ts.net:8792/setup?token=<token>` and choose the password. The token file is deleted once the password is set.

**Content bootstrap.** Once both AI keys are in the env file, the supervisor notices that the fixed phrases are missing (about a minute after start) and runs `python -m zozobox.content bootstrap` by itself: the phrases, the evergreen fallbacks and the soundboard starter pack. Follow it with `journalctl -u app-zozobox -g bootstrap -f`. It respects the spend cap; if it stops early, the supervisor tries again after 12 hours while phrases are still missing. To run it now instead, see "Run content by hand" under Operating.

**Settings and approvals.** Log in to the dashboard. On the settings page, set the **Time zone** to the box's zone (the same as `timedatectl` shows), the location (weather) and the quiet hours. **Approve the fixed phrases**, the soundboard clips and the evergreen items. Until you do, the box plays no spoken phrases; this is the first-month approval rule (SPEC 7.1). The nightly run (about 02:30) makes new content each night, and that content also waits for approval during the first month. The first nightly run is at the first 02:30 after the first start.

### Soundboard clips

`bootstrap` TTS-generates the starter pack: 16 hand-reviewed silly clips (animal sounds, a robot, giggles, nursery rhymes), stored like the phrases in the content store. The texts are in `zozobox/content/soundboard.py`. They are TTS output, so in the first month each clip waits on the **Approvals** page like every other generated item (SPEC 7.1), and the soundboard plays only the approved ones. Until you approve some or import your own, Yellow plays nothing but the click. SPEC 2.3 asks for bundled CC0 sounds; those can't be redistributed from this repo, so the TTS pack stands in and your imported CC0 clips complete it. To add real recordings:

1. On [freesound.org](https://freesound.org), search, then under **License** tick **Creative Commons 0**. CC0 needs no attribution. We still record the source.
2. Prefer short clips (1 to 10 s), gentle sounds and no sudden loud parts. Listen to the whole clip before adding it.
3. Copy it to the Pi and import it as the service user. This loudness-normalizes it to -16 LUFS, 48 kHz mono, and logs it in `soundboard/CREDITS.tsv`:
   ```bash
   ~/apps/daily-panel/.venv/bin/zozobox-import-clip ~/cow.flac \
       --source https://freesound.org/s/12345/ --license CC0 --author someone
   ```
   Imported clips go to the directory the engine plays from (`[app].soundboard_dir` in `box.toml`, default `~/.local/share/zozobox/soundboard`) and need no approval: you chose and listened to them. Clips longer than 20 s are refused (`--max-s` changes that). The engine picks up new files on the next press.
4. To remove an imported clip, delete its file from `soundboard/imported/`. To remove a starter clip, delete it on the dashboard.

### Fallback phrases (optional)

The engine looks for each fixed phrase in the content store first. If it isn't there, it looks for `~/.local/share/zozobox/assets/phrases/<name>.wav` (for example `sleepy.wav`). You can record your own voice for any phrase there. Normalize it first: `~/apps/daily-panel/.venv/bin/python -c "from zozobox.audio import normalize; normalize('in.m4a', 'sleepy.wav')"`. The names are listed in [ENGINE.md](ENGINE.md).

## 9. Dashboard access: Tailscale only

The dashboard binds `127.0.0.1:8791` only. `git-deploy` publishes it with `tailscale serve --https=8792`, so it is reachable **only from the tailnet**, over HTTPS with a real certificate, at `https://<pi>.<tailnet>.ts.net:8792/` (SPEC 8). It is not on the home LAN, and you should never open a router port for it. The session cookie is marked `Secure`.

- In the Tailscale admin console, **disable key expiry** for the Pi, so the box never drops off the tailnet while nobody is watching.
- Limit access to the parents' devices with an ACL. For example, allow only `group:parents` to reach `tag:zozobox:8792`, and tag the Pi `tag:zozobox`. On a shared pi-ops Pi, scope the rule to port 8792 so it does not open the other apps.
- Never serve the box on 443: on the pi-ops Pi that port is the public Funnel for `/hooks`.
- Login limits: 5 wrong passwords per parent per 5 minutes, 20 in all. Behind `tailscale serve` every request reaches the dashboard from `127.0.0.1`, so the per-parent limit is keyed on the `Tailscale-User-Login` header that `tailscale serve` adds (and replaces if a client sends it), or, for a tagged device without a user, the last `X-Forwarded-For` address. One parent mistyping does not lock out the other.

## 10. Pulling the plug

Pulling the plug must be safe (SPEC 1). The data side is: SQLite runs in WAL mode with fsyncs, and media files and state files are written to a temp file and renamed, so a power cut loses at most the last moment. The old read-only overlay root is **not used** with pi-ops: every deploy writes the clone (`~/apps`), pi-ops' state (`~/.local/state/git-deploy`) and the unit (`/etc/systemd/system`), and an overlay would throw all of that away at the next reboot. The root filesystem is a normal ext4 journal filesystem.

Optional hardening, for an SD card you want to protect further: put the data on its own ext4 partition, mounted at the data dir. Stop the unit, copy the data across, then mount it with `commit=5`:

```
LABEL=zozodata  /home/bash102/.local/share/zozobox  ext4  defaults,noatime,commit=5  0  2
```

(`sudo mkfs.ext4 -L zozodata /dev/mmcblk0p3`; `sudo systemctl stop app-zozobox`; mount it at `/mnt`, `sudo cp -a ~/.local/share/zozobox/. /mnt/`, unmount, `sudo mount ~/.local/share/zozobox`, `sudo chown bash102: ~/.local/share/zozobox`, `chmod 700` it; `sudo systemctl start app-zozobox`.)

## Parent shutdown, and a sudo-free alternative

Holding red and yellow for 5 s makes the box chime: two beeps if a sync pass reached the cloud in the last 5 minutes, one otherwise (offline, or sync not running). The LEDs then blink, and the engine runs `sudo -n /usr/bin/systemctl poweroff`. The sudoers drop-in from `host-setup.sh` allows that command for the service user; it is what makes the chord work on a Pi where the user has no other sudo. The unit that pi-ops renders has no `NoNewPrivileges=`, so sudo works from it.

### Privileges (accepted risk)

Under pi-ops the box does **not** run as a confined user. Every child (engine, dashboard, sync, the content and mood jobs) runs as `bash102`, which pi-ops needs to have passwordless `sudo systemctl` (and `sudo tailscale`) for git-deploy's restarts from the webhook and the timers. Passwordless `sudo systemctl <anything>` is root-equivalent, and pi-ops' unit has no sandboxing (`NoNewPrivileges=`, `ProtectSystem=` and so on). So a code-execution bug in the tailnet-reachable dashboard is root on the Pi, and the poweroff-only drop-in confines nothing. The old standalone install used a dedicated `zozobox` user with heavy systemd hardening; pi-ops (read-only for this project) renders one fixed unit and has no way to add either. This is recorded as an accepted risk (SPEC 8). What limits it: the dashboard is only on the tailnet (ACL it to the parents' devices, step 9), it needs the parent password, and logins are rate-limited. To remove the risk, pi-ops would need to support a separate app user or unit hardening; raise that with the pi-ops owner.

If you prefer polkit to sudo:

1. Add `/etc/polkit-1/rules.d/50-zozobox-poweroff.rules` (for `bash102`):
   ```js
   polkit.addRule(function (action, subject) {
     if (subject.user == "bash102" &&
         (action.id == "org.freedesktop.login1.power-off" ||
          action.id == "org.freedesktop.login1.power-off-multiple-sessions")) {
       return polkit.Result.YES;
     }
   });
   ```
2. In `box.toml`, set `[app] shutdown_command = ["/usr/bin/systemctl", "poweroff"]`, and restart the unit.
3. Remove `/etc/sudoers.d/zozobox-poweroff`.

## Operating

Run these on the Pi as the service user.

| Task | Command |
|---|---|
| Status of the deploy | `git-deploy status daily-panel` |
| Follow all logs | `journalctl -u app-zozobox -f` |
| One child's lines | `journalctl -u app-zozobox -g '^engine:'` (or `dashboard:`, `sync:`, `nightly:`, `mood:`, `bootstrap:`) |
| Engine status (which hardware came up) | `journalctl -u app-zozobox -g "engine ready"` |
| What the supervisor runs or skips | `cd ~/.local/share/zozobox && (set -a; . ~/.config/zozobox/env; ~/apps/daily-panel/.venv/bin/python -m zozobox.run --plan)` |
| Restart everything (after editing the env file or `box.toml`) | `sudo systemctl restart app-zozobox` |
| Stop the box (bench tests, BRINGUP.md) | `sudo systemctl stop app-zozobox` |
| Deploy now | `git-deploy sync daily-panel` |
| Hold deploys, for example during a family visit | `git-deploy pause daily-panel --reason "Grandma visiting"`; afterwards `git-deploy resume daily-panel`, then `git-deploy sync daily-panel` |
| After a `blocked` or `unhealthy` deploy is fixed | `git-deploy unblock daily-panel`, then `git-deploy sync daily-panel` (unblock alone deploys nothing) |
| One sync pass | `sudo systemctl restart app-zozobox` (sync runs a pass at start) |
| Engine "failed" after a `box.toml` edit | `journalctl -u app-zozobox -g '^engine:' -n 20` shows the config error (exit 2, retried every 5 minutes); fix it, then `sudo systemctl restart app-zozobox` |
| The box restarted by itself | `journalctl -u app-zozobox -g 'oom\|Failed with result'`: `oom-kill` means a process hit `memory_max` and systemd stopped the whole unit (`OOMPolicy=stop`). Check `systemctl show -p MemoryPeak app-zozobox`, then lower `ZOZOBOX_JOB_MEMORY_MB` or raise `memory_max` |
| "started 2 times without finishing" | A job was cut off twice (OOM or restarts); it is skipped until its next slot. Find the cause as above; to run it now, use "Run content by hand" |
| "code changed on disk since start" | A deploy is installing, or it failed and the app is blocked: `git-deploy status daily-panel`, fix the cause, `git-deploy unblock daily-panel`, `git-deploy sync daily-panel` |

While a deploy is paused the box keeps running exactly as it is; `git-deploy status` shows `PAUSED`. A paused or blocked deploy never stops the running unit.

**Run content by hand.** The supervisor's scheduled runs and a manual one never overlap (content holds a lock; a second run reports `locked`). Load the env file first:

```bash
zbox() { (cd ~/.local/share/zozobox && set -a && . ~/.config/zozobox/env && set +a &&
          ~/apps/daily-panel/.venv/bin/python -m "$@"); }
zbox zozobox.content status
zbox zozobox.content nightly          # tonight's content now
zbox zozobox.content bootstrap        # phrases, evergreen, soundboard pack
```

**Updating** is merging to `main`. Never `git pull`, edit or `git reset` inside `~/apps/daily-panel`: a local change blocks deploys with "worktree has local changes", and pi-ops' README explains how to recover. Changes to `deploy/host-setup.sh` need a manual re-run (`sudo ~/apps/daily-panel/deploy/host-setup.sh`); git-deploy does not run it.

## Moving from the old standalone install

Earlier versions installed to `/opt/zozobox`, `/etc/zozobox` and `/var/lib/zozobox` with their own `zozobox-*` units and a `zozobox` system user. To move a box over:

1. `sudo systemctl disable --now zozobox-engine zozobox-dashboard zozobox-sync zozobox-nightly.timer zozobox-mood-reprocess.timer`, then `sudo rm /etc/systemd/system/zozobox-* && sudo systemctl daemon-reload`. `host-setup.sh` warns while they are still installed.
2. Do steps 2 to 4. Copy the old config and secrets: `sudo cat /etc/zozobox/box.toml > ~/.config/zozobox/box.toml` and the four values from `/etc/zozobox/secrets.env` into `~/.config/zozobox/env`.
3. Copy the data: `sudo cp -a /var/lib/zozobox/. ~/.local/share/zozobox/ && sudo chown -R bash102: ~/.local/share/zozobox`. Leave the old `[app]` paths out of `box.toml` (they default to the new data dir).
4. Do steps 6 and 7. Once the box works, remove `/opt/zozobox`, `/etc/zozobox`, `/var/lib/zozobox` and the `zozobox` user (`sudo userdel zozobox`). `host-setup.sh` has already rewritten `/etc/sudoers.d/zozobox-poweroff` for the service user.

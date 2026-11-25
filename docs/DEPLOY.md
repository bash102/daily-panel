# Deploying Zozo's box

This guide covers a fresh Raspberry Pi 3 through to a running box. The hardware checks that need a person standing at the box are in [BRINGUP.md](BRINGUP.md).

Everything on the Pi runs as the `zozobox` system user, from these paths:

| Path | What |
|---|---|
| `/opt/zozobox/src` | This repo. It is read-only for the services. |
| `/opt/zozobox/venv` | The Python virtualenv (`pip install -e .[pi]`). |
| `/etc/zozobox/box.toml` | Static box config: pins, cards, ceilings, printer. The template is `deploy/box.toml.example`. |
| `/etc/zozobox/secrets.env` | API keys and the cloud token. `root:root`, mode `0600`. Read by systemd only. |
| `/var/lib/zozobox` | All data: the SQLite DB, media, soundboard, chimes and the printer lock. Put it on the persistent partition. |

## 1. Flash the OS

1. Flash **Raspberry Pi OS Lite (64-bit or 32-bit), Bookworm** with Raspberry Pi Imager. In the Imager settings, set the hostname (for example `zozobox`), an admin user with an SSH key, Wi-Fi, and the **timezone of the box**. The system timezone drives the nightly timer (about 02:30). Quiet hours, the box day and the grandma review window use the box's own **Time zone setting on the dashboard** (default `America/New_York`), which you set to the same zone in step 7.
2. Boot, then SSH in. Run `sudo apt update && sudo apt full-upgrade -y && sudo reboot`.
3. Check the clock: `timedatectl` should show `System clock synchronized: yes`. The Pi has no RTC, and quiet hours and the review window depend on the time being right.

## 2. Run install.sh

```bash
sudo apt install -y git
sudo git clone <this repo> /opt/zozobox/src
sudo /opt/zozobox/src/deploy/install.sh
```

`install.sh` is idempotent. Re-run it after every `git pull`; it restarts the services that are running, and once the secrets are filled in it also starts the timers (a re-run prints any service that is not running). It does the following:

- Installs the apt packages: `python3-venv python3-dev alsa-utils ffmpeg libusb-1.0-0 fonts-dejavu-core i2c-tools git python3-lgpio`.
- Enables I2C (`raspi-config nonint do_i2c 0`).
- Creates the `zozobox` system user and adds it to the `gpio i2c audio lp plugdev` groups.
- Creates the venv at `/opt/zozobox/venv`, runs `pip install -e /opt/zozobox/src[pi]`, and byte-compiles the package (the services cannot write to `/opt/zozobox/src`). When the checkout is not already at `/opt/zozobox/src`, it copies it there in full, so files removed upstream go away.
- Generates the click and beep chimes into `/var/lib/zozobox/chimes/`.
- Installs `box.toml` and `secrets.env` **only if they don't exist yet**. It never overwrites them.
- Installs `/etc/asound.conf`, but only if there isn't one. Otherwise it writes `asound.conf.zozobox-new` next to it.
- Installs the udev rule for the QL-800 (`04f9:209b`, group `lp`).
- Installs the sudoers drop-in `/etc/sudoers.d/zozobox-poweroff`, after checking it with `visudo`. It allows only `systemctl poweroff`.
- Installs the systemd units, runs `daemon-reload`, and enables `zozobox-engine`, `zozobox-dashboard`, `zozobox-sync`, `zozobox-nightly.timer` and `zozobox-mood-reprocess.timer`.

Options: `--no-apt` skips apt, `--no-pip` skips pip, `--offline` is both (a re-run with no network at all; new Python dependencies then wait for a run with network), and `--no-enable` installs without enabling.

Reboot once after the first install so that I2C and the group memberships take effect.

### 2a. Set the dashboard password right after that reboot

The dashboard is enabled and listens on port 8080 on every interface from the first boot. Until a parent password exists, anyone who can reach it could choose that password, so `/setup` only works with a one-time token. The token is in a root-readable file and in the dashboard's log:

```bash
sudo cat /var/lib/zozobox/dashboard-setup-token
journalctl -u zozobox-dashboard -g setup     # the same token, in the setup link
```

Open `http://<pi>:8080/setup?token=<token>` and choose the password. The token file is deleted once the password is set. Do this before you bring up Tailscale (step 8), so the setup page is never reachable from the tailnet.

## 3. Secrets

Edit the secrets with `sudo nano /etc/zozobox/secrets.env`. Keep it `root:root 0600`: systemd reads it as root and passes the values to the services.

| Variable | Where it comes from |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic Console. Create a **separate key** in a "zozobox" workspace and set a workspace spend limit (SPEC 7.0). |
| `OPENAI_API_KEY` | OpenAI. Create a key in a separate project and set a project budget. |
| `ZOZOBOX_CLOUD_URL` | The worker's `PUBLIC_BASE_URL` (step 5), an `https://` URL. `zozobox-sync` refuses to start (and never sends the token) while this or the token is still `replace-me`, or when the URL is not https. |
| `ZOZOBOX_BOX_TOKEN` | The same value as the worker's `BOX_TOKEN` secret. |

## 4. box.toml

`/etc/zozobox/box.toml` holds the wiring and devices. Every key is optional and the defaults are listed in the file. Check your edits with:

```bash
cd /tmp && sudo -u zozobox /opt/zozobox/venv/bin/python -m zozobox.app --check-config
```

(The `cd /tmp` matters for every `sudo -u zozobox` command in these docs: the `zozobox` user cannot read your home directory, and Python and lgpio both use the current directory.)

A typo, such as an unknown key, a pin used twice or a role missing, makes the engine exit with status 2 and a clear message in `journalctl -u zozobox-engine`. The unit does not restart in a loop on that status (`RestartPreventExitStatus=2`): it stays **failed** until you fix the file and run `sudo systemctl restart zozobox-engine`. The dashboard reads the same file for reprints (`[printer]`, `[app].printer_lock_timeout_s`) and behaves the same way, so restart both after a change. The values you will most likely change during bring-up:

- `[buttons].pins`: BCM pin numbers.
- `[leds].channels`: PCA9685 channels.
- `[leds].invert`: flip it if brightness comes out backwards.
- `[audio.handset|speaker|mic].card`: the ALSA short names from `aplay -l`. Change them in `/etc/asound.conf` too.
- `mixer_control`
- `max_fraction`: the volume ceilings.
- `[printer].identifier`

Then run `sudo systemctl restart zozobox-engine zozobox-dashboard`.

## 5. Cloudflare Worker

Follow [cloud/worker/README.md](../cloud/worker/README.md), "Deploy". You can do this from any computer with Node 22 or later, not just the Pi. In short:

1. Run `npx wrangler login`.
2. Create the D1 database and copy its id into `wrangler.toml`, then apply the migrations.
3. Create the R2 bucket `zozobox-archive`.
4. Set `PUBLIC_BASE_URL`.
5. Set the secrets: `TWILIO_*`, `GRANDMA_NUMBER`, `PARENT_NUMBER`, `GRANDMA_PIN`, `BOX_TOKEN` and `MEDIA_SIGNING_KEY`.
6. Run `npx wrangler deploy`.

Put the same `BOX_TOKEN` into `secrets.env` as `ZOZOBOX_BOX_TOKEN`. Check that the Pi can reach the worker:

```bash
curl -H "Authorization: Bearer $ZOZOBOX_BOX_TOKEN" "$ZOZOBOX_CLOUD_URL/api/health"
sudo systemctl restart zozobox-sync && journalctl -u zozobox-sync -n 20
```

## 6. Twilio

1. Buy a US local voice-capable number in the Twilio console (about $1.15/month).
2. Under Phone Numbers, open the number. Set **A call comes in** to Webhook, `POST https://<worker host>/twilio/voice`. Leave the SMS webhook empty.
3. The texts (to grandma when Zozo's reply is published, and to the parent on a PIN lockout) are sent from this number. For US numbers, complete Twilio's A2P 10DLC registration, or texts may be filtered.
4. Test: call the number, enter the PIN, press 2 and leave a message. Within a sync pass (30 s) the Grandma LED should be bright. [BRINGUP.md](BRINGUP.md) has the full call test.

## 7. Content bootstrap and first approvals

```bash
sudo systemctl start zozobox-bootstrap      # phrases, evergreen fallbacks, soundboard pack
journalctl -u zozobox-bootstrap -n 50
```

Then open the dashboard at `http://<pi>:8080` and log in (the password is from step 2a). On the settings page, set the **Time zone** to the box's zone (the same as `timedatectl` shows), the location (weather) and the quiet hours. Quiet hours, the box day and the review window use this setting, not the system clock's zone. **Approve the fixed phrases**, the soundboard clips and the evergreen items. Until you do, the box plays no spoken phrases; this is the first-month approval rule (SPEC 7.1). The nightly run (`zozobox-nightly.timer`, about 02:30) makes new content each night, and that content also waits for approval during the first month.

### Soundboard clips

`bootstrap` TTS-generates the starter pack: 16 hand-reviewed silly clips (animal sounds, a robot, giggles, nursery rhymes), stored like the phrases in the content store. The texts are in `zozobox/content/soundboard.py`. They are TTS output, so in the first month each clip waits on the **Approvals** page like every other generated item (SPEC 7.1), and the soundboard plays only the approved ones. Until you approve some or import your own, Yellow plays nothing but the click. SPEC 2.3 asks for bundled CC0 sounds; those can't be redistributed from this repo, so the TTS pack stands in and your imported CC0 clips complete it. To add real recordings:

1. On [freesound.org](https://freesound.org), search, then under **License** tick **Creative Commons 0**. CC0 needs no attribution. We still record the source.
2. Prefer short clips (1 to 10 s), gentle sounds and no sudden loud parts. Listen to the whole clip before adding it.
3. Copy it to the Pi and import it. This loudness-normalizes it to -16 LUFS, 48 kHz mono, and logs it in `soundboard/CREDITS.tsv`. The `zozobox` user cannot read your home directory (mode 0700), so put a world-readable copy in `/tmp` first:
   ```bash
   install -m 0644 ~/cow.flac /tmp/cow.flac
   cd /tmp && sudo -u zozobox /opt/zozobox/venv/bin/zozobox-import-clip /tmp/cow.flac \
       --source https://freesound.org/s/12345/ --license CC0 --author someone
   rm /tmp/cow.flac
   ```
   Imported clips go to the directory the engine plays from (`[app].soundboard_dir` in `box.toml`, default `/var/lib/zozobox/soundboard`) and need no approval: you chose and listened to them. Clips longer than 20 s are refused (`--max-s` changes that). The engine picks up new files on the next press.
4. To remove an imported clip, delete its file from `soundboard/imported/`. To remove a starter clip, delete it on the dashboard.

### Fallback phrases (optional)

The engine looks for each fixed phrase in the content store first. If it isn't there, it looks for `/var/lib/zozobox/assets/phrases/<name>.wav` (for example `sleepy.wav`). You can record your own voice for any phrase there. Normalize it first: `/opt/zozobox/venv/bin/python -c "from zozobox.audio import normalize; normalize('in.m4a', 'sleepy.wav')"`. The names are listed in [ENGINE.md](ENGINE.md).

## 8. Tailscale (dashboard away from home)

The dashboard listens on port 8080 on every interface. On the home LAN it is protected by the parent password (set it first, step 2a). To reach it from outside, use Tailscale. Don't open a port on the router.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname zozobox
```

- In the Tailscale admin console, **disable key expiry** for this machine, so the box never drops off the tailnet while nobody is watching.
- Limit access to the parents' devices with an ACL. For example, allow only the group `group:parents` to reach `tag:zozobox:8080`, and tag the Pi `tag:zozobox`.
- With the read-only root (next section), Tailscale's state dir `/var/lib/tailscale` must live on the data partition. See below.

## 9. Read-only root and the persistent data partition

Pulling the plug must be safe (SPEC 1). Do this last, once everything above works, because it makes `/etc` read-only too. These steps are documented but not automated: they repartition the SD card.

1. **Create a data partition.** Shrink the root partition, or flash to a larger card and leave free space. Then add an ext4 partition, for example `/dev/mmcblk0p3`. Doing this from another Linux machine with `gparted` is easiest.
   ```bash
   sudo mkfs.ext4 -L zozodata /dev/mmcblk0p3
   ```
2. **Mount it** at `/var/lib/zozobox`. Add this line to `/etc/fstab`:
   ```
   LABEL=zozodata  /var/lib/zozobox  ext4  defaults,noatime,commit=5  0  2
   ```
   Then copy the existing data across:
   ```bash
   sudo systemctl stop 'zozobox-*'
   sudo mount LABEL=zozodata /mnt
   sudo cp -a /var/lib/zozobox/. /mnt/ && sudo umount /mnt
   sudo mount /var/lib/zozobox && sudo chown zozobox:zozobox /var/lib/zozobox
   ```
   The units have `RequiresMountsFor=/var/lib/zozobox`, so they wait for the mount. SQLite runs in WAL mode with fsyncs, and media files are written with temp files and renames, so a power cut loses at most the last moment.
3. **Keep other state that must persist** on the data partition, using bind mounts in `/etc/fstab`:
   ```
   /var/lib/zozobox/.system/tailscale  /var/lib/tailscale  none  bind  0  0
   ```
   (`sudo mkdir -p /var/lib/zozobox/.system/tailscale`, and copy the existing state there first.) The ALSA mixer state is fine in the lower layer: run `sudo alsactl store` after calibration, before you enable the overlay.

   **The clock.** The Pi 3 has no RTC. Pi OS keeps the time across reboots with `fake-hwclock`, which saves it to `/etc/fake-hwclock.data`. Under the overlay those saves are thrown away, so every boot would start from the time the overlay was enabled (possibly weeks old) until NTP syncs, and an offline boot would run quiet hours, the box day and event timestamps on that stale date. Keep the file on the data partition instead:
   ```bash
   sudo mkdir -p /var/lib/zozobox/.system
   sudo fake-hwclock save && sudo cp /etc/fake-hwclock.data /var/lib/zozobox/.system/
   echo 'FILE=/var/lib/zozobox/.system/fake-hwclock.data' | sudo tee -a /etc/default/fake-hwclock
   sudo systemctl edit fake-hwclock     # add the two lines below
   ```
   ```
   [Unit]
   RequiresMountsFor=/var/lib/zozobox
   ```
   After the next reboot, check `systemctl status fake-hwclock` (it should have loaded the file without errors) and `date`. If the boot ordering gives you trouble, an I2C RTC module (DS3231) is the robust fix. The engine and dashboard units are ordered after `time-sync.target`; that only waits for a real NTP sync if `systemd-time-wait-sync.service` is enabled, and offline it never delays the start.
4. **Enable the overlay:** `sudo raspi-config nonint enable_overlayfs`, then reboot. Check with `findmnt /`, which should show `overlay`. The boot partition can be made read-only as well, in the raspi-config Performance menu under Overlay.
5. **Making changes later** (for example `box.toml`, `secrets.env`, `git pull` and `install.sh`): run `sudo raspi-config nonint disable_overlayfs`, reboot, make the change, then enable the overlay again and reboot.
6. The journal is in RAM under the overlay, so logs are lost on reboot. The event log in the store keeps the box's own history.

## Parent shutdown, and a sudo-free alternative

Holding red and yellow for 5 s makes the box chime: two beeps if a sync pass reached the cloud in the last 5 minutes, one otherwise (offline, or `zozobox-sync` not running). The LEDs then blink, and the engine runs `sudo -n /usr/bin/systemctl poweroff`. The sudoers drop-in allows exactly that command, and nothing else, for the `zozobox` user. sudo is setuid, so the engine unit cannot use `NoNewPrivileges=` or any hardening option that implies it. The other units use them.

If you prefer polkit to sudo:

1. Add `/etc/polkit-1/rules.d/50-zozobox-poweroff.rules`:
   ```js
   polkit.addRule(function (action, subject) {
     if (subject.user == "zozobox" &&
         (action.id == "org.freedesktop.login1.power-off" ||
          action.id == "org.freedesktop.login1.power-off-multiple-sessions")) {
       return polkit.Result.YES;
     }
   });
   ```
2. In `box.toml`, set `[app] shutdown_command = ["/usr/bin/systemctl", "poweroff"]`.
3. Remove the sudoers drop-in.
4. Add `NoNewPrivileges=yes` to `zozobox-engine.service` with a drop-in: `systemctl edit zozobox-engine`.

## Operating

| Task | Command |
|---|---|
| Follow all logs | `journalctl -u 'zozobox-*' -f` |
| Engine status (which hardware came up) | `journalctl -u zozobox-engine -g "engine ready"` |
| Run tonight's content now | `sudo systemctl start zozobox-nightly` |
| Content status | `cd /tmp && sudo -u zozobox /opt/zozobox/venv/bin/python -m zozobox.content status` |
| Engine stays failed after a `box.toml` edit | `journalctl -u zozobox-engine -n 20` shows the config error (exit 2, no restart loop); fix it, then `sudo systemctl restart zozobox-engine` |
| One sync pass | `sudo systemctl restart zozobox-sync` (it syncs at start) |
| Update | `cd /opt/zozobox/src && sudo git pull && sudo deploy/install.sh` |
| Update without network | `sudo deploy/install.sh --offline` |

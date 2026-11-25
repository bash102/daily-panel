# Hardware bring-up checklist (M11)

This checklist is for the owner, at the real box, after [DEPLOY.md](DEPLOY.md) steps 1 to 8. Work through it in order and tick each box. Run every command as the pi-ops service user (the user that owns `~/apps/daily-panel`). Whenever a step changes `~/.config/zozobox/box.toml`, run `~/apps/daily-panel/.venv/bin/python -m zozobox.app --check-config`, then `sudo systemctl restart app-zozobox`.

Stop the box before you use the bench tool, because the engine owns the pins and the audio devices. `app-zozobox` is one unit for the engine, the dashboard and sync, so they all stop; start it again when you are done (`sudo systemctl start app-zozobox`). A deploy while it is stopped would start it again, so hold deploys for the session:

```bash
git-deploy pause daily-panel --reason "bring-up bench tests"
sudo systemctl stop app-zozobox
bench() { (cd ~/.local/share/zozobox && LG_WD=$PWD ~/apps/daily-panel/.venv/bin/zozobox-bench "$@"); }
```

The `cd` and `LG_WD` matter: gpiozero's lgpio pin factory writes small notification files into its working directory, and they must not land in the clone (`~/apps/daily-panel`), where they would block deploys. Afterwards: `sudo systemctl start app-zozobox`, then `git-deploy resume daily-panel` and `git-deploy sync daily-panel`.

## 1. Wiring (SPEC 1, 1.1)

- [ ] **Buttons.** Wire each button from its pin to GND. There are no external resistors; the Pi's internal pull-ups are used. The default BCM pins are red 17, green 27, blue 22 and yellow 23 (`[buttons].pins`). Never use GPIO 2 or 3 (I2C) or 14 and 15 (UART).
- [ ] **PCA9685 power.** The PCA9685 **VCC goes to the Pi's 3.3V** (pin 1), not 5V. V+ and every LED anode go to the 5V rail. GND is common.
- [ ] **I2C.** SDA goes to GPIO 2 (pin 3), SCL to GPIO 3 (pin 5).
- [ ] **LEDs.** Each LED cathode goes to a PWM pin: red 0, green 1, blue 2, yellow 3 (`[leds].channels`).
- [ ] **LED resistors.** Size the reworked resistors for 10 to 20 mA, counting the on-board 220Ω: R_total = (5V − Vf) / I.
- [ ] **I2C check.** `sudo i2cdetect -y 1` shows `40`. If it doesn't, check VCC, SDA/SCL and that I2C is enabled.
- [ ] **USB.** The handset, the speaker DAC and the QL-800 are plugged in, and the printer has its own power adapter.
- [ ] **Handset cord.** It is at most 30 cm long, with strain relief.

## 2. LEDs and the invert check

- [ ] Run `bench leds --hold 2`. Each LED steps through off, dim, bright, slow pulse and fast pulse in turn.
- [ ] **Invert check.** "off" must be fully dark, "dim" a faint glow and "bright" full brightness. If off is bright and bright is dark, set `[leds] invert = false`. If off still glows faintly, the outputs are not open-drain: check `open_drain = true` and the wiring (SPEC 1.1).
- [ ] **Flicker.** The pulse states show no visible flicker. Also check with a phone camera; if needed, raise `pwm_hz`, up to 1500.
- [ ] **Balance.** The four colours look equally bright when dim. If not, use `[leds].trim` (for example `{ blue = 0.7 }`), and `dim_level` if dim is too bright or too faint.

## 3. Buttons

- [ ] Run `bench buttons`. Each press prints the right colour and role.
- [ ] Mash one button fast: about one press per second is accepted (the lockout), and nothing repeats while you hold it.
- [ ] Hit two buttons at once: only the first is reported.
- [ ] Hold red and yellow for 5 s: `PARENT CHORD` is printed once.
- [ ] If a colour is wrong, fix `[buttons].pins`. If you want a different layout, change `[roles]`.

## 4. ALSA card discovery

- [ ] Run `aplay -l` and `arecord -l`. Note the short name in brackets for each card, for example `card 1: Phone [POP Phone]` gives `Phone`. Check that the handset shows up as both a playback and a capture device (SPEC 9.2).
- [ ] Run `amixer -c <card> scontrols` for each card. Note the playback control (`PCM`, `Speaker`, `Headphone`...) and the mic control (`Mic`, `Capture`...).
- [ ] Put the names into `~/.config/zozobox/box.toml`: `[audio.handset].card` and `.mixer_control`, `[audio.speaker]`, and `[audio.mic].card` and `.mixer_control`. Use `"none"` if a card has no control. Its ceiling then comes only from asound.conf.
- [ ] Put the same card names into `/etc/asound.conf`: `hw:CARD=...` and `card ...` in both blocks. For each output, check its channel count with `aplay -D hw:CARD=<card> --dump-hw-params /dev/zero`. If it only takes 1 channel, set `channels 1` in that dmix block.
- [ ] Test each PCM: `aplay -D zozobox_handset ~/.local/share/zozobox/chimes/beep.wav` and the same with `zozobox_speaker`. (The engine writes the chimes at its first start.)
- [ ] Set the softvol caps to 100%: `amixer -c <handset card> sset 'Zozobox Handset Cap' 100%` and `amixer -c <speaker card> sset 'Zozobox Speaker Cap' 100%`. The controls appear after the first play. Then run `sudo alsactl store`.
- [ ] Check the handset's pick-up button: `sudo evtest`. Note whether it shows up as a key. v1 ignores it (SPEC 9.2).

## 5. Volume ceiling calibration (hearing safety, SPEC 5)

You need a sound level meter, or a calibrated meter app, set to dBA and slow response. The target is **75 to 80 dBA at the earpiece**, held against the meter as it would be against an ear. Do the box speaker at Zozo's usual distance.

- [ ] Use a real speech asset as the calibration clip, for example an evergreen story chunk: `ls ~/.local/share/zozobox/media/evergreen/story/*/chunk_0.wav | head -1`. Every asset is normalized to -16 LUFS, so speech is representative.
- [ ] Run `bench audio --volume 1.0 --file <that clip>`. This plays at the **maximum** dashboard volume, which is the worst case.
- [ ] Measure the earpiece. If it is louder than 80 dBA, first lower `max_dB` in the handset softvol block of `/etc/asound.conf` (the hard cap, in 3 dB steps), then fine-tune `[audio.handset].max_fraction`. Repeat until the peak is 80 dBA or less.
- [ ] Do the same for the speaker: `[audio.speaker].max_fraction` and the speaker `max_dB`.
- [ ] Check that a wrong card name means silence, not loud audio: temporarily misspell `mixer_control`, and `bench audio` should refuse to play on that output. Then restore it.
- [ ] Run `sudo alsactl store` once more. Write the final numbers into `box.toml` and keep a note of the meter readings.

## 6. Microphone threshold

- [ ] `bench audio --seconds 4`: say "happy" at a toddler's distance and volume. The output shows `silent=False` and the p90 level.
- [ ] Run it again and stay quiet: `silent=True`, and p90 is clearly below `speech_dbfs` (default -40 dBFS).
- [ ] If speech is reported silent, lower `[audio.mic].speech_dbfs` (for example to -45) or raise `capture_fraction`. If room noise counts as speech, raise it.
- [ ] Check that the box speaker is silent during the recording (no feedback) and comes back afterwards.

## 7. Printer (QL-800)

- [ ] **Turn Editor Lite off.** Hold the Editor Lite button until its green light goes out. Otherwise the printer appears as a USB disk.
- [ ] `lsusb | grep 04f9:209b` shows the printer, and `ls -l /dev/bus/usb/*/*` shows its node with group `lp` (from the udev rule). If it doesn't, run `sudo udevadm trigger` and replug the printer.
- [ ] Load DK-22205 (62 mm continuous) paper.
- [ ] `bench print all` prints a letter card, a weather card, the four-faces mood card and a coloring page, each cut. Check that each card is exactly 62 mm wide and crisp, with nothing cut off.
- [ ] Unplug the printer and run `bench print mood`: it prints `FAILED ... no USB device`, with no crash. Plug it back in.
- [ ] With the box running (`sudo systemctl start app-zozobox`), unplug the printer and press Print: you hear "printer sleepy". Plug it back in, wait about 1 minute (the retry), and press Print again: the card prints.
- [ ] Do a dashboard reprint while the engine prints. The cards come out one after the other, not garbled (the shared lock).

## 8. Engine end-to-end

- [ ] `sudo systemctl start app-zozobox`, then `journalctl -u app-zozobox -g "engine ready"`. The output should say `leds=ok, buttons=ok, audio=ok, printer=ok, mood_classifier=ok`. Anything `failed` is named, with its reason.
- [ ] Every accepted press clicks on the box speaker.
- [ ] Print goes through the rotation letter → weather → mood → coloring, and each prints once. A fifth press replays the audio with no card. The Print LED is bright until all four have printed.
- [ ] **Mood:** answer "happy", and you hear "You feel happy!" and get the happy face. Stay silent, and you get four faces. Unplug the network and answer: you get four faces, and within the hour the supervisor's mood reprocess job labels the take (`journalctl -u app-zozobox -g '^mood:'`).
- [ ] Story plays. Pressing again skips a chunk. Once the story is heard, the Story LED goes dim.
- [ ] Soundboard plays a different clip each press.

## 9. Grandma line (Twilio) with the PIN

- [ ] Call the Twilio number from grandma's phone. Enter the wrong PIN 3 times: the call hangs up, and the parent gets a text. Wait out the lockout (1 hour), or test from another phone.
- [ ] Call again with the right PIN and press 2. Leave a message.
- [ ] Within about 30 s (one sync pass), the Grandma LED is bright. Press Grandma: her message plays on the handset and the speaker, then "Say hi to Grandma!", a beep, and recording (fast pulse).
- [ ] Say something and press Grandma to stop. The dashboard shows Zozo's reply with its review countdown.
- [ ] Delete one reply during the window and check it is never published. Let another run out: grandma gets a text. She calls, presses 1, and hears it.

## 10. Sync

- [ ] `journalctl -u app-zozobox -g '^sync:' -f` shows no errors (sync only logs warnings and errors). The dashboard shows the sync status as online.
- [ ] Prints, Zozo's recordings and mood takes appear in the R2 bucket under `prints/`, `recordings/zozo/` and `mood/`.
- [ ] Pull the network for 10 minutes, use the box, then reconnect. The outbox drains, and nothing is lost.
- [ ] Hold red and yellow for 5 s while online. You hear two beeps, the LEDs blink, and the Pi powers off cleanly. Offline (or with sync not running for more than 5 minutes, for example while the cloud values in the env file are placeholders), you hear one beep.

## 11. First-month approvals

- [ ] Right after bootstrap, approve all the fixed phrases and the evergreen items on the dashboard. Until then the box plays no speech, only clicks and beeps.
- [ ] Every morning for the first month, review and approve the night's story, letter, weather and coloring page. Anything not approved is not served; the evergreen fallback is used instead.
- [ ] Check the dashboard's safety-flag and spend panels once a week. The cap is $30/month.

## 12. Quiet hours

- [ ] On the dashboard, set quiet hours to start 2 minutes from now.
- [ ] When they start, all LEDs go off. A press plays "Shh, the box is sleeping" once, and further presses within 5 minutes are silent. Nothing prints, and Story is blocked.
- [ ] A grandma reply recorded just before quiet hours does not publish during them: its review window pauses.
- [ ] Set the real quiet hours back.

## 13. Toddler slap test

- [ ] Slap all four buttons with a flat hand, repeatedly, for 30 s. The box never crashes or locks up, and plays at most about one thing per second, without stacking sounds.
- [ ] Slap during each experience: during a print (the card still prints, or the next press replays), during the grandma recording (the reply is saved and the slapped button starts), and during the mood question.
- [ ] Yank the handset cord gently and knock the box: nothing comes loose, and the cord holds.
- [ ] Pull the power plug while it is playing, then plug it back in. The box boots, the engine starts, and the data is intact: yesterday's prints and messages are still on the dashboard.
- [ ] Afterwards, `journalctl -u app-zozobox -g '^engine:.*(Traceback|ERROR)'` shows nothing unexpected. Note anything odd in docs/PLAN.md.

When everything here is ticked, set M11 to done in [PLAN.md](PLAN.md). Remember to resume deploys if you paused them (`git-deploy resume daily-panel`).

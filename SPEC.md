# Zozo's Button Box — Specification (v1)

Interactive button box for Zoriaz ("Zozo"), age 2–3, running on a Raspberry Pi 3.
Design principle: every interaction is a single press, and the box must survive slapping.

## 1. Hardware

| Part | Decision |
|---|---|
| Computer | Raspberry Pi 3 |
| Buttons | 4 illuminated arcade buttons: red, green, blue, yellow. Wired straight to Pi GPIO using internal pull-ups (no MCP23017). |
| Button LEDs | Reworked from 12V to 5V (swap or bypass the built-in resistors), PWM dimmed. |
| LED driver | **Adafruit PCA9685 16-channel 12-bit PWM breakout (#815)**, https://www.adafruit.com/product/815. Talks to the Pi over I2C (address 0x40) using `adafruit-circuitpython-pca9685` via Blinka. Generates its own PWM, so the Pi's 2 hardware PWM channels (shared with the 3.5mm jack) stay free. A breakout rather than a HAT, so the GPIO header stays free for the buttons and hook switch. See the wiring below. |
| Handset | USB retro phone handset (earpiece + mic in one USB audio device), no hook switch. |
| Box speaker | Secondary speaker used as speakerphone and for chimes. Likely needs its own USB DAC + 2–3W amp. |
| Printer | Brother QL-800, continuous 62mm paper (no stickers). Driven with `brother_ql`. |
| Power | One 5.1V 3–4A supply for the Pi and LED driver; the printer uses its own adapter. Root filesystem runs as an overlay so pulling the plug is safe. |
| Enclosure | Wall-mounted. Printer inside the box behind a ~5mm finger-proof slot with a parent-access lid and ventilation. Handset on a coiled cord no longer than 30cm, with strain relief. |

### 1.1 LED driver wiring (PCA9685)
- **VCC (logic) → Pi 3.3V.** The board's I2C pull-ups then go to 3.3V, which is safe for the Pi.
- **V+ and the LED anodes → 5V rail.** Do not power the logic from 5V.
- **Each LED cathode → a PWM pin (channels 0–3).** The pins sink current. The outputs tolerate 5.5V and sink up to 25mA each.
- **Outputs in open-drain mode** (OUTDRV = 0 in the MODE2 register). In the default totem-pole mode, an "off" pin drives 3.3V, which leaves ~1.7V across the LED and can make it glow faintly. Check the OUTDRV/INVRT table in the NXP datasheet. If brightness comes out inverted, fix the duty cycle in software.
- **LED current:** keep it at ~10–20mA. Each output already has a 220Ω resistor on the board, so size the reworked LED resistors with that included: R_total = (5V − Vf) / I.
- **PWM frequency:** ≥ 1kHz to avoid visible flicker (the PCA9685 goes up to ~1.5kHz).
- **Why not the AW9523:** its LED supply has to match its logic supply. On the Pi that means 3.3V, which leaves blue LEDs (Vf ~3.0–3.2V) with too little headroom.

## 2. Buttons

### 2.1 Print — rotation: Letter → Weather → Mood → Coloring page → (loop)
Each item is a multi-step audio sequence that ends in a print.
- **Letter of the day:** announce the letter → repeat-after-me vocabulary ("B… ball! Can you say ball?"; prompt + pause, no speech recognition) → print a letter card with a drawing to color.
- **Weather:** spoken summary ("Sunny and cold, wear your jacket") → print a weather icon. The location is set on the dashboard.
- **Mood:** "How are you feeling?" → record → cloud speech-to-text matches the answer to one of **happy, mad, sad, neutral**.
  - Recognized: "You feel happy!" → print that face.
  - Spoke but not recognized: ask once more → print all four faces, log `unknown`.
  - Silent: print all four faces.
  - Offline: queue the audio, recognize it later, keep the original timestamp.
  - Every run is logged with its audio, transcript, label and source. The first mood of the day counts as "today's mood".
- **Coloring page:** a generated line-art page.

**Print rules**
- Pressing Print while an item is playing does nothing. The next press after the item finishes moves to the next item. An item prints only if its sequence completed.
- Each item prints at most **once per day**. After that the item still plays its audio, as often as he likes, and skips the print.
- The rotation restarts at Letter each morning.
- Reprints from the parent dashboard are unlimited, don't count toward the daily limit, and are archived as separate print events.

### 2.2 Grandma — voicemail-like
1. Play grandma's latest message. If none exists yet, skip this step.
2. "Say hi to Grandma!" + beep.
3. Record his reply. Recording stops at the dashboard-set cap (default 30s, max 5 min) or on any button press.
   - Any non-empty partial recording is saved. The pressed button's own action then starts, unless it was Grandma.
   - An empty or silent recording is discarded and the previous message is kept.
4. The reply replaces the single live slot. After a **1-hour** review window, measured from recording time, it auto-publishes to grandma's line and texts her. A parent can delete it during the window. The cloud enforces the window.
5. Grandma's messages play to Zozo without review. Parents can delete them on the dashboard.
6. Pressing Grandma during playback skips straight to the beep.

### 2.3 Soundboard
- Plays a random clip from a bundled starter pack (CC0 animal sounds, silly voices, songs).
- Avoids repeating recent clips.
- Pressing again plays the next clip.

### 2.4 Story
- A ~60s story with Zozo as the main character, generated overnight.
- Pressing again skips to the next chunk.
- Today's story replays as often as he likes.
- **Content rules:** age appropriate for 2–3, teaches good values, never scary, focused on positive experiences.

## 3. Interaction rules (slap-proofing)
- 20ms debounce.
- When several buttons are hit at once, the first one wins.
- Other buttons are ignored for about 1s after an action starts. Within that limit, pressing a different button switches to it, subject to the rules above.
- Repeated presses of the same button skip at most about once per second, except on Print (see 2.1).
- Every accepted press plays a short click on the box speaker.
- Holding red and yellow for 5s is a hidden parent gesture for safe shutdown and a network-status chime.

## 4. LEDs
| State | Meaning |
|---|---|
| Bright steady | Something new: unheard grandma message, unheard story, a Print item not yet printed today. |
| Dim steady | Available. Soundboard is always dim. |
| Slow pulse | Playing |
| Fast pulse | Recording |
| Off | Quiet hours |

## 5. Audio
- Speech plays on both the handset earpiece and the box speaker. Chimes play on the box speaker only.
- The box speaker mutes while the mic is recording, to avoid echo and feedback.
- All audio is loudness-normalized to -16 LUFS.
- Each output has a fixed hardware/ALSA volume ceiling that is hearing-safe (about 75–80 dBA at the earpiece). The dashboard volume adjusts only below the ceiling.

## 6. Quiet hours
- Set on the dashboard.
- LEDs off, no printing, Story blocked.
- A press plays one soft "Shh, the box is sleeping" clip, at most once every 5 minutes. Presses are logged.
- The Grandma review window pauses during quiet hours.

## 7. Backend

### 7.1 Content generation (cloud AI, nightly)
- Pre-generates the story (text + text-to-speech), line art, letter vocabulary and audio. The Pi only plays and prints cached files.
- **Only regenerate what was served.** An unserved story, letter or coloring page carries over unchanged and keeps its "new" LED.
- Weather is an inexpensive API call, not generation, so it is refreshed every night or morning regardless.
- **Safety gate:**
  - A fixed safety system prompt.
  - A second AI moderation pass on text and a safety check on images.
  - Retry once on failure, then fall back to pre-vetted evergreen content.
  - Failures are flagged on the dashboard.
  - **For the first month every item also needs parent approval on the dashboard** before it can be served. After that, approval is automatic, and parents can still flag or delete items.
- **Privacy:** AI providers only get "Zozo" and the age range, with no-retention and no-training API settings.

### 7.2 Grandma phone line (Twilio, fully in the cloud)
- A US local number (~$1.15/mo). It works even when the Pi is offline.
- **PIN-only access** (4 digits). After 3 wrong tries, hang up, lock out, and text a parent.
- Menu: press 1 to hear Zozo's latest published message; press 2 to record a new one.
- The Pi syncs with the cloud in both directions. Twilio's copies are deleted once they are confirmed archived.

### 7.3 Archive (cloud is the primary copy)
- A permanent record of every story, grandma recording, Zozo recording, mood entry and log event, plus a copy of every print (the rendered image).
- **Cloudflare R2** (the owner's existing account) is the source of truth. The Pi caches recent items and queues uploads while offline.
- Audio is stored as Opus. The index keeps metadata.
- Deleting or overwriting only stops publishing. The archive keeps the item with flags and timestamps, including grandma messages he never heard. **Purge permanently** is a separate action that needs confirmation.

### 7.4 Offline behavior
- The box runs from its cache, uploads wait in a queue, and mood answers are recognized later. The grandma line keeps working in the cloud.

### 7.5 Spend cap
- About **$30/month** across AI, speech-to-text, Twilio and storage.
- When it's reached, nightly generation pauses and he gets cached and evergreen content. The dashboard alerts you.

## 8. Parent dashboard
- Hosted on the Pi. Reachable on the home network and over Tailscale (access limited to parent devices). Password required.
- Features:
  - Reprint anything.
  - Browse and play the archive.
  - Statistics and the mood log.
  - Review or delete his message, and delete grandma messages.
  - Approve generated content (first month).
- Settings:
  - Quiet hours.
  - Weather location.
  - Recording cap (max 5 min).
  - Volume (below the ceiling).
  - Spend cap.
- Shows disk use, sync status and safety-check flags.

## 9. Open items
1. ~~LED driver part selection~~ → decided: Adafruit PCA9685 (#815).
2. Confirm the handset has no hook switch.
3. Cloud services still to choose: relay host (Cloudflare Workers is a natural fit next to R2), AI, text-to-speech, speech-to-text. Archive storage = Cloudflare R2 (decided).

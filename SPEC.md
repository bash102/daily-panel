# Zozo's Button Box — Specification (v1)

Interactive button box for Zoriaz ("Zozo"), age 2–3, running on a Raspberry Pi 3.
Design principle: every interaction is a single press, and the box must survive slapping.

## 1. Hardware

| Part | Decision |
|---|---|
| Computer | Raspberry Pi 3 |
| Buttons | 4 illuminated arcade buttons: red, green, blue, yellow. Wired straight to Pi GPIO using internal pull-ups (no MCP23017). |
| Button LEDs | Reworked from 12V to 5V (swap or bypass the built-in resistors), PWM dimmed. |
| LED driver | **Adafruit PCA9685 16-channel 12-bit PWM breakout (#815)**, https://www.adafruit.com/product/815. Talks to the Pi over I2C (address 0x40) using `adafruit-circuitpython-pca9685` via Blinka. Generates its own PWM, so the Pi's 2 hardware PWM channels (shared with the 3.5mm jack) stay free. A breakout rather than a HAT, so the GPIO header stays free for the buttons (and an optional future hook switch). See the wiring below. |
| Handset | **Native Union POP Phone (USB-C)**, purchased. Earpiece and mic in one USB audio device. Connects to the Pi 3 through a USB-C(female) to USB-A(male) adapter. **No hook switch in v1**: the box can't tell whether the handset is lifted, so speech always plays on both the handset and the box speaker. Its 3.2m coiled cord must be shortened, or held inside the box, to the ≤30cm limit. Its pick-up/hang-up button may show up in Linux as an input key; that's unverified, and if it does it will be ignored or used as a parent shortcut. Optional later upgrade: an Adafruit magnetic contact switch (#375, ~$4) under the handset rest, with a magnet on the handset, on one GPIO input. |
| Box speaker | Secondary speaker used as speakerphone and for chimes. Likely needs its own USB DAC + 2–3W amp. |
| Printer | Brother QL-800, continuous 62mm paper (no stickers). Driven with `brother_ql`. |
| Power | One 5.1V 3–4A supply for the Pi and LED driver; the printer uses its own adapter. No read-only overlay root (it would discard pi-ops' deploy state on reboot); pulling the plug is made safe by SQLite WAL and atomic writes, with an optional separate data partition (docs/DEPLOY.md). |
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

### 7.0 Cloud services (decided)
| Job | Service | Notes |
|---|---|---|
| Story text, letter vocabulary, weather phrasing, mood classification | **Anthropic Claude API**, `claude-opus-5-5` | Python `anthropic` SDK. Adaptive thinking. Mood labels come back through structured output as an enum: happy, mad, sad, neutral, unknown. Enable server-side refusal fallbacks (`fallbacks: "default"`). The nightly job can go through the Message Batches API at half price. |
| Safety check on text **and images** | **Claude** (`claude-opus-5-5`, vision) | An independent second call that reviews each story and each generated image against the content rules in 2.4 and 7.1. A different vendor from the image generator, so one model's blind spots aren't checked by the same model. |
| Coloring pages and letter-card drawings | **OpenAI image API** (currently `gpt-image-2`) | Prompted for thick black line art on a white background. The Pi converts the result to 1-bit at the QL-800's 62mm / 300 dpi width with Pillow. Claude can't generate images, so this needs OpenAI. |
| Text-to-speech (stories, prompts, fixed phrases) | **OpenAI TTS** | Use one fixed warm, gentle voice everywhere so Zozo hears one consistent "box voice". Fixed phrases ("Say hi to Grandma!", "Shh, the box is sleeping") are generated once and cached permanently. Pick the current model and voice when building. |
| Speech-to-text (mood answers) | **OpenAI transcription** (`gpt-4o-mini-transcribe`, ~$0.003/min) | The transcript goes to Claude for the mood label. Toddler speech will often fail; the `unknown` path in 2.1 covers that. |
| Archive | **Cloudflare R2** | See 7.3. |
| Twilio relay, review timer, SMS | **Cloudflare Workers** (with R2) | Handles the Twilio webhooks, the PIN check, the 1-hour publish timer and the SMS. Works while the Pi is offline. |
| Weather | A free weather API (e.g. Open-Meteo, no key needed) | Fetched daily; Claude turns it into toddler-friendly phrasing. |

**Keys:** reuse the existing Anthropic and OpenAI accounts (already on the Pi for another project), but create a **separate API key per project** for the button box. Group them in an Anthropic Console workspace and an OpenAI project, and set provider-side spend limits there (Anthropic workspace limit, OpenAI project budget). That makes the $30/month cap (7.5) enforceable and the bill traceable.

**Where the nightly job runs:** on the Pi, as a scheduled Python job using the keys above. Generation needs the internet anyway. The Worker only handles Twilio, R2 and timers.

**Data retention:** both providers keep API data for a limited time for abuse monitoring by default, and neither trains on it. True zero retention needs approval from each provider. Only "Zozo", the age range and mood clips are sent.

**Estimated monthly cost:**
- Claude: ~$3–6
- OpenAI images: ~$2–6 (about 2 per day)
- TTS: < $1
- Speech-to-text: < $0.50
- Twilio: ~$2–3
- R2 and Workers: free tier

Total about $10–15/month, under the $30 cap.

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
- Audio is stored as **WAV in v1** (decided at M10). Twilio's `<Play>` cannot play Opus, and grandma's and Zozo's messages must stay playable on the phone line straight from the archive (the Worker cannot transcode). At this volume (short voice clips) the R2 free tier covers years of storage. The index keeps metadata. Revisit only if storage grows well beyond that.
- Deleting or overwriting only stops publishing. The archive keeps the item with flags and timestamps, including grandma messages he never heard. **Purge permanently** is a separate action that needs confirmation.

### 7.4 Offline behavior
- The box runs from its cache, uploads wait in a queue, and mood answers are recognized later. The grandma line keeps working in the cloud.

### 7.5 Spend cap
- About **$30/month** across AI, speech-to-text, Twilio and storage.
- When it's reached, nightly generation pauses and he gets cached and evergreen content. The dashboard alerts you.

## 8. Parent dashboard
- Hosted on the Pi. Reachable **over Tailscale only** (access limited to parent devices), published with `tailscale serve` by the pi-ops deployment; the dashboard itself listens on localhost. Not reachable on the home network: pi-ops serves apps to the tailnet only, Tailscale gives HTTPS with a real certificate and device-level access control for free, and nothing on the LAN (guests' devices, IoT gadgets) can reach the login or first-run setup page. Parents' phones and laptops run Tailscale. Password required.
- Accepted risk (pi-ops): the whole box, the dashboard included, runs as the pi-ops service user `bash102`, which has passwordless `sudo systemctl` for git-deploy, so it is effectively root, and pi-ops' unit has no sandboxing. A code-execution bug in the dashboard would be root on the Pi. Tailscale-only access, the password and the login limits (per parent, keyed on the Tailscale identity) are what keep it out of reach; the old dedicated, sandboxed `zozobox` user is not available under unmodified pi-ops.
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
2. ~~Handset~~ → decided: Native Union POP Phone (USB-C), no hook switch in v1. Still to do: confirm Linux detects it as USB audio (`aplay -l`, `arecord -l`) and check whether its button shows up as an input key (`evtest`).
3. ~~Cloud services~~ → decided (see 7.0). At build time: confirm the current OpenAI image, TTS and transcription model names and prices.

# Toddler Button Box (Pi 3): Handsets and LED Drivers

Date: Nov 2026. Everything below comes from the verified findings. Anything not confirmed is marked **UNVERIFIED**. Prices are approximate and stock changes.

---

## 1. Handsets with hook or call detection

### 1a. USB / wired

| Name | Price | How the hook is detected | Pi notes | Link |
|---|---|---|---|---|
| **Native Union POP Phone (USB-C, 2025 re-release)** | $39.99 US; A$59.95; £34.99 | No cradle. One pick-up/hang-up call button. Linux reporting of the button (HID key) is **UNVERIFIED**. | Needs a USB-C(f) to USB-A(m) adapter. No battery, no pairing. Linux UAC support is **UNVERIFIED** but likely, since it works driverless on laptops. The 320 cm coiled cord is a strangulation risk: shorten or enclose it. | https://www.nativeunion.com/en-hk/products/pop-phone · https://www.verizon.com/products/native-union-pop-phone-usb-c-wired-headphone |
| **DIGITUS DA-70772 USB handset** | **UNVERIFIED** (~$20-30) | None (no cradle, no confirmed answer key). Add a cradle with a switch on GPIO. | USB-A plug goes straight into the Pi 3. Vendor lists Linux support. Office look, not retro. | https://assmann.com/produkte/video-und-audio/audio-konverter-und-zubehoer/da-70772/?PL=en |
| **StarTech UR9-HEADSET-ADAPTER (USB-C to RJ9)** | ~$42-47 | None. It is an audio adapter for a donor handset. Wire the donor cradle's hook to GPIO. | Linux is not listed (**UNVERIFIED**, likely UAC). RJ9 pinouts vary. Levels with retro handsets are **UNVERIFIED**. | https://www.cdw.com/product/startech-usb-c-to-rj9-adapter-dongle/8438273 |
| **Crosley CR62 Kettle Classic (donor)** | ~$30-65 black; ~$110 chrome | Real mechanical cradle hook switch. NO/NC type and pinout are **UNVERIFIED**. Solder 2 wires to GPIO. | Not USB. Audio goes through a USB sound card or the StarTech adapter. A desk phone with a tethered handset. | https://www.walmart.com/ip/16829811 |
| **RDYMONKEY Audio Guestbook Phone** | **UNVERIFIED** (~$73-86 earlier) | Built-in lift-to-record hook. Internals and NO/NC are **UNVERIFIED**. | Works standalone with no Pi. As a Pi part it is only a donor (gut it). Has a rechargeable battery. Sparse reviews. | https://www.walmart.com/ip/17153270580 |
| *Software:* rotary-phone-audio-guestbook | Free | `hook_gpio` and `hook_type` (NC/NO) in config.yaml. Example uses GPIO 22. | Handles off-hook play/record over USB audio, so it avoids the PWM and 3.5 mm pins. Compatibility with the current Pi OS is **UNVERIFIED**. | https://github.com/nickpourazima/rotary-phone-audio-guestbook |

### 1b. Bluetooth

On every Bluetooth option the Pi must act as the **HFP Audio Gateway** (BlueALSA or oFono). Hook and button events arrive as AT commands (ATA, AT+CHUP, AT+CKPD), often only during a faked call. Audio is narrowband, and handsets need charging.

| Name | Price | How the hook is detected | Pi notes | Link |
|---|---|---|---|---|
| **Generic BT retro handset (e.g. ENJOY-UNIQUE B075SXTKPF)** | $13-22 | No cradle. Pick-up/hang-up button sent as HFP/HSP AT commands. | Handset only, so a toddler can walk off with it. About 4 h charge, 12 h talk. Linux AT behaviour is **UNVERIFIED**. Some "retro handsets" are wired-only, so check the listing. | https://manuals.plus/asin/B075SXTKPF |
| **Target "Retro Style Wireless Handset Rotary Dial Phone"** | $16.99-19.99 | Has a cradle, but whether lifting answers or ends calls is **UNVERIFIED**. The dial is probably decorative. | Tethered handset in pink or black. Rated 2.6/5, with a low-volume complaint. Power and BT specs are **UNVERIFIED**. | https://www.target.com/p/retro-style-wireless-handset-rotary-dial-phone-with-hd-audio/-/A-93282034 |
| **BT rotary-style desk phone (Amazon B0H4KJBY3L)** | **UNVERIFIED** | Lift-to-answer cradle is claimed but **UNVERIFIED**. | Lead only. Check the Q&A for "lift handset to answer" before buying. | https://www.amazon.com/dp/B0H4KJBY3L |
| **Generic BT desk phone with base and speakerphone** | **UNVERIFIED** (~$120-125 seen) | Hang-up signalling varies by model (**UNVERIFIED**). | The highest-effort and least predictable option. | https://www.walmart.com/c/kp/bluetooth-phones |
| **VTech Connect to Cell DECT (IS8122-3, DS6621-2)** | $98-135 | Charging cradles. Whether a cradle lift is sent as ATA/CHUP to Linux is **UNVERIFIED**. | Durable, but modern-looking and expensive. Fallback only. | https://www.staples.com/vtech-connect-to-cell-3-handset-cordless-telephone-silver-black-is8122-3/product_24496884 |
| Native Union Moshi Moshi MM03 Curve | Used only | Base is a real hang-up switch, and lifting answers (confirmed by the manual). | Discontinued about 2010, so the battery is likely aged. Low priority. | https://manuals.online/native-union/telephone-accessories/mm03/user-manual/1wk7u |
| Pyle PITLBT20/30 | Discontinued | The dock probably only charges. | Skip. | https://www.bhphotovideo.com/c/product/903547-REG/Pyle_Home_pitlbt20_Bluetooth_Wireless_Charging_Retro.html |
| Native Union POP Phone **Bluetooth** | Not sold | — | Dropped. The current POP line is USB-C wired only. | — |

---

## 2. Add-a-hook options for hookless handsets

All of these are passive contacts. Wire GPIO to GND, turn on the internal pull-up and read it with `gpiozero.Button(bounce_time≈0.05)`. They work with any handset, including a sealed Bluetooth one, if you add a magnet or the handset rests on the switch.

| Option | Price | Mechanism | Notes | Link |
|---|---|---|---|---|
| **Adafruit Magnetic Contact Switch (PID 375)** | $3.95 | NO reed switch plus a magnet block, closes at ~13-15 mm | **Lowest effort.** Glue the magnet to the handset and put the reed under the rest spot. Sealed, and nothing for a toddler to pinch. Stock varies by region. | https://www.adafruit.com/products/375 |
| Adafruit Hall sensor US5881LUA (PID 158) | $2 + magnet | Open-drain Hall switch on 5 V; pull up to **3.3 V only** | Tiny and hides behind a wall. Needs 3 wires and the correct magnet pole. | https://www.adafruit.com/products/158 |
| Adafruit roller microswitch (PID 819) | ~$2-3.76 (Adafruit price **UNVERIFIED**) | Snap-action COM/NO/NC switch | Needs no magnet, but the lever is a pinch point, so hide it. A light handset may not press it. | https://www.adafruit.com/products/819 |
| G-TEL Cradle / Reed Switch + JP Magnetic Handset (HS-MAG) | $21.95 + $29.95 | Reed cradle plus matching magnet handset | Most complete kit and built for abuse. Payphone look with a stiff, heavy cord. NO/NC and audio element specs are **UNVERIFIED**. | https://payphone.com/Cradle-Reed-Switch-Assembly.html |
| G-TEL Cradle-Hookswitch (90-12504-25) | $25.50 | Mechanical hook operated by handset weight | Rugged, but a pinch risk. Fit with non-payphone handsets is **UNVERIFIED**. | https://payphone.com/Cradle-Hookswitch-Assembly.html |
| Vintage WE 500 hookswitch (eBay donor) | ~$25-40 used | Built-in leaf contacts; find the pair with a multimeter | Authentic look, but the most effort: rewire the base and usually swap in an electret mic and speaker. | https://oldphoneworks.com/products/black-model-500 |

---

## 3. LED driver boards to review

All of these leave the Pi 3's hardware PWM and 3.5 mm audio pins alone (I2C or SPI only). For LEDs, wire anode to 5 V and cathode to the driver pin (sinking).

| Board | Ch | Drives 5 V LEDs directly? | Interface / library | Form factor | Price | Link |
|---|---|---|---|---|---|---|
| **Adafruit AW9523 GPIO Expander + LED Driver (4886)** | 16 | Yes. Constant-current sink, 256-step. Defaults to **37 mA max**, so set IMAX to 1/2 (~18 mA) or cap the dim value. | I2C 0x58, STEMMA QT. `adafruit-circuitpython-aw9523` | Small breakout | ~$4.95 | https://thepihut.com/collections/led-drivers-backpacks/products/adafruit-aw9523-gpio-expander-and-led-driver-breakout |
| **Adafruit PCA9685 16-ch PWM Driver (815)** | 16 | Yes, 25 mA sink. Onboard 220 Ω series resistors. | I2C 0x40. `adafruit-circuitpython-pca9685` | Breakout, header stays free | $14.95 | https://www.adafruit.com/product/815 |
| Adafruit 16-ch PWM/Servo **Bonnet** (3416) | 16 | Yes, 25 mA sink. Use open-drain mode. Series resistors **UNVERIFIED**. | I2C 0x40. Same library | Bonnet covers the header | ~$12.50 | https://thepihut.com/products/adafruit-16-channel-pwm-servo-bonnet-for-raspberry-pi-ada3416 |
| Adafruit TLC59711 (1455) / TLC5947 (1429) | 12 / 24 | Yes. Constant current, ~15 mA by default (3.3K Rset), 16-bit. | SPI (data+clk). `adafruit-circuitpython-tlc59711` | Breakout | ~$7.50-9 | https://thepihut.com/products/adafruit-12-channel-16-bit-pwm-led-driver-spi-interface-tlc59711 |
| Waveshare Servo Driver HAT (15275) | 16 | Yes, 25 mA sink. Series resistors **UNVERIFIED**. | I2C. Waveshare demo code; Adafruit library **UNVERIFIED** | HAT | $14.99 | https://www.waveshare.com/servo-driver-hat.htm |
| Pimoroni Explorer HAT Pro (PIM082) | 4 out + 4 in | Yes. ULN2003 sinking, 500 mA total. Software PWM only. | GPIO. `explorerhat` | HAT | ~$24-30 | https://thepihut.com/products/explorer-hat |
| Generic PCA9685 clone | 16 | Yes, 25 mA sink. May lack resistors, so add your own. | I2C 0x40. Adafruit library | Breakout | ~$2-8 | https://www.robotshop.com/en/pca9685-16-channel-12-bit-pwm-servo-driver.html (**UNVERIFIED** URL) |
| Seeed Grove PCA9685 (108020102) | 16 | Yes, 25 mA sink | Grove I2C. Default 0x7F and Pi support **UNVERIFIED** | Grove module | ~$9.50-17, patchy stock | https://wiki.seeedstudio.com/Grove-16-Channel_PWM_Driver-PCA9685/ |
| *Skip:* Pimoroni Automation HAT Mini | 3 | Only 3 outputs, no dimming | — | — | — | https://shop.pimoroni.com/products/automation-hat-mini |
| *Skip:* Adafruit DC/Stepper Motor HAT (2348) | 4 | Awkward: H-bridges, separate supply | — | — | $22.50 | https://www.adafruit.com/products/2348 |

---

## 4. Top recommendations

**Handset (overall): Native Union POP Phone (USB-C) plus an Adafruit 375 reed switch in a simple cradle.** It is the only currently sold retro handset with no battery, no pairing and no audio soldering, and the project already sends audio over USB. It plugs into the Pi 3 through a C-to-A adapter. Add the reed switch under a printed or wooden cradle and glue the magnet to the handset, and you get a real lift-to-listen, hang-up-to-stop hook. The hook is a sealed, finger-safe contact read on one GPIO with near-zero latency. The rotary-phone-audio-guestbook code gives you the play/record logic. Main risk: the 3.2 m coiled cord must be shortened or captured inside the box for a 2-3 year old. Linux enumeration is **UNVERIFIED**, so buy from a retailer with easy returns. Fallback with a factory hook: a Crosley CR62 donor with its hook switch on GPIO and a USB sound card. Recordings can optionally be synced to your Cloudflare R2 bucket.

**Bluetooth (if wireless is a must): a generic BT retro handset (~$15) plus the same reed-switch cradle.** I don't recommend this path for a toddler. It means charging, sleep or wake delays, narrowband audio, and setting up the Pi as an HFP audio gateway whose button events may only arrive during a faked call. Using a GPIO reed switch for the hook instead of AT commands removes the least reliable part. The base-style Target and B0H4KJBY3L phones are tempting because the handset is tethered, but their hook behaviour is **UNVERIFIED**.

**Add-a-hook: Adafruit Magnetic Contact Switch (PID 375).** At $3.95 it is passive, needs no power pin, has no moving lever to pinch toddler fingers, and works with any handset, wired or Bluetooth. Keep a spare.

**LED driver: Adafruit AW9523 (4886).** It is the cheapest board here, at about $5. It drives 4 LEDs at constant current from the 5 V rail with smooth 256-step dimming and needs no resistors or transistors. It connects over 4 I2C wires (STEMMA QT), so the header stays free and the PWM/audio pins are untouched. It can also read the 4 buttons. Set IMAX to 1/2 (~18 mA) so a software bug can't push 37 mA through 10-20 mA LEDs. Runner-up: the **Adafruit PCA9685 (815)**, with 12-bit PWM and built-in 220 Ω resistors on a breakout that leaves the header free. Size the LED resistors to allow for those 220 Ω.

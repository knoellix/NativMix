# NativMix Arduino Examples

> **Status:** These examples are shipped as reference implementations and are **not
> fully tested** in daily use yet. Hardware wiring, MIDI CC assignments, and LED
> feedback should be verified on your setup before relying on them in production.

## nativmix_midi_controller.ino

Example MIDI controller for NativMix with RGB LED feedback.

### Required Hardware

| Component | Qty | Notes |
|---|---|---|
| SparkFun Pro Micro (ATmega32u4) | 1 | Native USB-MIDI — Leonardo works too |
| Potentiometer / fader 10 kΩ | 4 | Linear taper |
| Momentary push button | 6 | 4× mute, 2× profile |
| Toggle switch | 1 | Direct profile activate |
| WS2812B RGB LED | 6 | Chainable, single data wire |
| 330 Ω resistor | 1 | In series on LED data line |
| 100 µF capacitor | 1 | Across LED power rail |

### Libraries (Arduino Library Manager)

- **MIDIUSB** — USB-MIDI for ATmega32u4 boards
- **Adafruit NeoPixel** — WS2812B control

### Pin Assignment

| Pin | Function |
|---|---|
| A0–A3 | Fader 1–4 |
| D2–D5 | Mute button 1–4 |
| D6 | Profile button: prev |
| D7 | Profile button: next |
| D8 | Profile switch (direct activate) |
| D9 | WS2812B data |

### MIDI Protocol

#### Sent by the controller

| CC | Value | Function |
|---|---|---|
| 1–4 | 0–127 | Fader volume (channel 1–4) |
| 5–8 | 127 | Mute toggle (channel 1–4) |
| 9 | 127 | Profile: previous |
| 10 | 127 | Profile: next |
| 11 | 127 | Profile: direct activate (switch) |

Assign these CCs in NativMix via MIDI-Learn.

#### Received from NativMix (LED feedback)

| CC | Value | Function |
|---|---|---|
| 32–37 | 0–127 | LED 0–5 color — hue encoding (see below) |
| 38 | 0–127 | Global LED brightness (optional) |

**Hue encoding** — CC value maps to color:

| Value | Color | Meaning (suggested) |
|---|---|---|
| 0 | Red | Muted / error |
| 21 | Orange | Warning |
| 42 | Green | Active / unmuted |
| 63 | Yellow | Fader takeover active |
| 85 | Blue | Idle / profile button |
| 106 | Purple | — |

The hue wraps around — 127 is back to red. Full saturation and brightness
are fixed in the firmware; use CC 38 to adjust brightness globally.

### USB Device Name

To make the controller show up as "NativMix Controller" in NativMix and
in system MIDI device lists, set the USB descriptor in the SparkFun Pro
Micro core (`boards.txt`):

```
promicro.build.usb_product="NativMix Controller"
promicro.build.usb_manufacturer="knoelliX"
```

NativMix can then match the device by name automatically.

### Extensibility

The protocol is intentionally open-ended:

- **More LEDs**: extend CC 32+ range, no firmware changes needed on the NativMix side
- **Displays**: MIDI SysEx is planned for streaming text data (app names,
  profile names, volume values) to attached OLED/LCD displays
- **More buttons**: any unused CC numbers can be assigned in NativMix via MIDI-Learn

### Bidirectional MIDI fader sync (opt-in)

NativMix can send outbound CC to move physical faders when volume changes in the
app (profile load, IPC `--vol`, GUI slider, external volume events). Enable it in
**Settings → Sync fader position to MIDI controller** (hybrid / MIDI-only modes;
default off).

The same toggle also drives **mute / LED feedback**:

| Outbound | When | Values |
|---|---|---|
| Learned mute CC | Mute changes in the app (GUI, IPC, MIDI button) | 127 = muted, 0 = unmuted |
| LED hue CC 32–35 | Mute CC is 5–8 (this sketch) | 0 = red (muted), 42 = green (unmuted) |

NativMix suppresses mute-toggle echoes for a short window after sending mute
feedback so the LED update does not re-trigger mute.

Implementation notes:

- **Feedback-loop protection:** when NativMix sends a CC to move a physical
  fader, the returning CC is ignored until the user moves the fader beyond a
  5 % deadband (similar to Arduino fader takeover / `--vol` IPC takeover).
- **Physical device required:** outbound sync uses the matching MIDI output port
  of the configured input device. The Linux virtual port receives inbound only —
  no outbound on virtual ports.
- **Throttling / dedupe:** identical CC values are not re-sent; inbound volume
  CC is throttled to 50 Hz per mapping.
- **Learn mode:** outbound sync is not paused during MIDI-Learn yet; disable the
  toggle temporarily if that interferes with learning on your controller.

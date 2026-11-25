# acroscope companion (Android)

The phone's side of acroscope (#75): collect the DVR clips and the blackbox log in the field, keep them on the
phone, hand them to the server at home. It does what the laptop's import script does, without the laptop.

What it does now (0.1):

- **Pick the event** (home, park, whoop race…). It becomes the session's name after the date; names used before,
  on this phone or on the server, are a tap away.
- **Opens on the plug.** Plugged in, the card's reader and the quad both bring up Android's "Choose an app for
  the USB device" with this app on it; chosen with **Always**, it opens by itself from then on.
- **Collect from the card.** The goggles' card in a USB-C reader is allowed once, then **Start** copies what is new
  on it (`DCIM/VID*.mov`, in the DVR's recording order). The card is only read, never written: it stays the second
  copy. What was collected is remembered by name, size and time and skipped next time. A clip without its index
  (the DVR lost power while recording) is collected and flagged. A collect that does not fit on the phone is
  refused before it starts. With nothing left to collect the card's line says the app has nothing open on it;
  **Eject the card…** opens the phone's own storage screen, where it is ejected (an app may not eject a card).
- **The log off the quad.** With the quad on USB, **Start** saves its blackbox flash first (MSP, as the
  Configurator's "save flash to file" does: the used part only), named
  `BTFL_BLACKBOX_LOG_<CRAFT>_<date>_<time>_<board>.bbl` with the phone's time. The file is kept only when it is as
  long as the flash said and starts like a log. After that, and only then, **Erase the quad's flash** is offered.
  Only one app can hold the flight controller: close the Betaflight app first. While the app is on the screen the
  quad's line says how much log is on its flash, or that the flash is empty: then there is nothing for **Start** to
  save, also right after an erase.
- **How warm the quad is.** On USB the board has power and no air over it. While a quad is plugged in and the app
  is on the screen, its MCU's core temperature is read every few seconds and shown; from 65 °C the line turns into
  a warning to unplug it, with a buzz and a tone every two seconds until it has cooled or is unplugged. With the
  app not on the screen nothing is read and nothing sounds, and the quad's line is free for another app (unless a
  saved log's erase is still on offer).
- **Add a saved log**: a `.bbl` saved by the Betaflight app's "save flash to file", for when the quad is not at
  hand. It keeps a name in the Configurator's pattern; under another name it is named from its own header.
- **Change it before it goes up**: the event, the date (it defaults to the day of the first collect), a file out,
  the whole batch away.
- **Upload**: every pending batch, oldest first, resumable from the last byte the server has. The phone's copy of a
  batch is deleted when the server reports it done, not when the upload ends: the server is looked in on
  afterwards, also with the app closed. A failed batch can be tried again on the server from here.
- **What the screen says stays true**: a finished job's line (collected, log saved, flash erased, uploaded) goes
  when the card or quad it was about is another one or gone, when a new collect starts, and when you leave the
  screen; what went wrong stays until the next try.
- **Upload by itself** (a switch, once the app is paired): when the phone is on a network that costs nothing and
  the server answers, what waits goes up without the app being opened, also after a restart of the phone. A run
  the phone cuts short, or one that finds no server (not at home), is tried again later; what went up stays. What
  only you can put right (the device revoked, a file gone) is said in a notification and not tried again.

Not yet: a QR code for pairing.

## Pairing

On the server's configuration page, devices tab: pair a device. Open the code's link on the phone, or paste the code
into the app ("pair with a server"). The code holds the server's address and this device's token; the app keeps both
in its own storage and talks to no other host.

## Layout

```
core/   the app's logic in plain Kotlin, no Android in it: names and rules shared with the server (Names), the
        index check (Mp4), the stored state (Store, Model), collecting (Collector), edits (Batches), the upload
        client (DropClient) and its orchestration (Uploader), MSP and the blackbox download (Msp, Blackbox)
app/    the Android application: card access through the storage access framework (Cards), the flight
        controller's USB serial port (Quads), the collect and the upload as WorkManager jobs (Workers), what the
        screen shows and asks for (Session, Screen)
```

The split is deliberate: everything in `core` is tested on any machine with a JDK, and `DropContractTest` runs the
upload client against a real `acroscope serve` child started from this checkout.

## Building

`scripts/setup.sh` puts a JDK 21 and the Android SDK under your home directory (no root, about 1.2 GB). Then:

```
export JAVA_HOME=~/.jdks/temurin-21 ANDROID_HOME=~/Android/Sdk
./gradlew --no-daemon :core:test :app:testDebugUnitTest :app:assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

The build is kept small in memory (`gradle.properties`); on a machine with little RAM use `--no-daemon` so nothing
stays behind, and do not run it next to the Python suite. The debug build installs as `org.acroscope.companion.debug`,
beside a release build, and is signed with the keystore in this folder (`debug.keystore`, the standard debug
password: it is not a secret and signs nothing else), so a build from any machine or from CI updates the one on the
phone without losing what it holds.

## Tests

- `:core:test`: the logic, and the client against the server (set `ACROSCOPE_PYTHON` to a Python that has
  acroscope's dependencies, else `.venv/bin/python` at the repo's root is used; without one those tests skip).
- `:app:testDebugUnitTest`: the screen driven as a pilot drives it, under Robolectric, with stand-ins for the card
  and the server; and the manifest's USB device filter, matched the way Android matches one.
- Only a phone can say: that Android offers the app on the plug, that the card in the reader is found and read
  (`SafCards`), that a flight controller's serial port opens and answers (`UsbSerial`; the protocol above it is
  tested against a stand-in controller), that the jobs survive leaving the app, how it feels in the hand. Those are
  checked on a real phone and said so in the PR.

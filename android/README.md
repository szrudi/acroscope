# acroscope companion (Android)

The phone's side of acroscope (#75): collect the DVR clips and the blackbox log in the field, keep them on the
phone, hand them to the server at home. It does what the laptop's import script does, without the laptop.

What it does now (0.1):

- **Pick the event** (home, park, whoop race…). It becomes the session's name after the date; names used before,
  on this phone or on the server, are a tap away.
- **Collect from the card.** The goggles' card in a USB-C reader is allowed once, then **Start** copies what is new
  on it (`DCIM/VID*.mov`, in the DVR's recording order). The card is only read, never written: it stays the second
  copy. What was collected is remembered by name, size and time and skipped next time. A clip without its index
  (the DVR lost power while recording) is collected and flagged. A collect that does not fit on the phone is
  refused before it starts.
- **Add a saved log**: a `.bbl` saved by the Betaflight app's "save flash to file". It keeps a name in the
  Configurator's pattern; under another name it is named from its own header.
- **Change it before it goes up**: the event, the date (it defaults to the day of the first collect), a file out,
  the whole batch away.
- **Upload**: every pending batch, oldest first, resumable from the last byte the server has. The phone's copy of a
  batch is deleted when the server reports it done, not when the upload ends. A failed batch can be tried again on
  the server from here.

Not yet: the log straight from the quad over USB, uploading by itself on Wi-Fi, a QR code for pairing.

## Pairing

On the server's configuration page, devices tab: pair a device. Open the code's link on the phone, or paste the code
into the app ("pair with a server"). The code holds the server's address and this device's token; the app keeps both
in its own storage and talks to no other host.

## Layout

```
core/   the app's logic in plain Kotlin, no Android in it: names and rules shared with the server (Names), the
        index check (Mp4), the stored state (Store, Model), collecting (Collector), edits (Batches), the upload
        client (DropClient) and its orchestration (Uploader)
app/    the Android application: card access through the storage access framework (Cards), the collect and the
        upload as WorkManager jobs (Workers), what the screen shows and asks for (Session, Screen)
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
  and the server.
- Only a phone can say: that the card in the reader is found and read (`SafCards`), that the jobs survive leaving
  the app, how it feels in the hand. Those are checked on a real phone and said so in the PR.

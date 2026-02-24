# Spotify Kodi Connect

Unofficial Spotify music addon for Kodi, inspired by the “Kodi Connect” style of PlexKodiConnect. Built on [plugin.audio.spotify](https://github.com/glk1001/plugin.audio.spotify) with improvements for consistent art, metadata, and smoother performance.

## Improvements over plugin.audio.spotify

- **Consistent art everywhere**  
  Album/artist/playlist art is set for all list types (thumb, poster, fanart, icon). Playlists and song lists no longer show missing art in some views.

- **Correct metadata**  
  Kodi music info is set with proper types (year, duration, tracknumber, discnumber, rating) and labels (albumartist for compilations, genre as string) so tagging and sorting work reliably.

- **Background precache**  
  Library precache runs in a background thread so the main menu appears immediately instead of blocking.

- **Smoother playlist play**  
  “Play playlist” starts the first track right away and queues the rest in the background so playback begins without a long wait.

- **Spotify Connect receiver (LibreSpot)**  
  Optional: enable “Connect receiver” in addon settings to run LibreSpot. Your Kodi device then appears as a speaker in the Spotify app on your iPhone or other devices—tap “Connect to a device” and choose it to stream and control playback from the phone while audio plays on Kodi. You can use a **bundled** librespot binary for a fully standalone addon: place the librespot binary in the addon’s `bin/` folder (e.g. `bin/librespot` on Linux/Android/CoreELEC, or `bin/librespot.exe` on Windows). If no binary is in `bin/`, the addon will try the system `librespot` (e.g. `opkg install librespot` on CoreELEC). Backends: PulseAudio RTP (Kodi plays the stream) or ALSA (direct to device).

## Requirements

- Kodi 19+ (Python 3)
- Spotify Premium
- Same dependencies as the original addon (script.module.requests, etc.)

## Installation

1. Install this addon (e.g. zip or copy into the addons folder).
2. Configure and authenticate once (same flow as the original Spotify addon).
3. Use **Music → Add-ons → Spotify Kodi Connect** (or your skin’s equivalent).

You can run this addon alongside the original `plugin.audio.spotify`; it uses a different addon id and proxy port (52309) and its own auth/cache.

## Troubleshooting: Connect receiver (LibreSpot) and service.librespot

- **Run only one LibreSpot instance.** If you have both **Spotify Kodi Connect** (with “Connect receiver” enabled) and the **service.librespot** addon installed, they both start at Kodi boot and each tries to run librespot. That can cause “failed to initialize” and the device not appearing in the Spotify app. **Fix:** Use either the Connect receiver inside this addon **or** service.librespot, not both. Disable the other (e.g. disable “Connect receiver” in this addon’s settings, or disable/remove the service.librespot addon).

- **“Failed to initialize” / “librespot failed 1/5” … “failed too many times”.** The real error is printed by the librespot binary. In the Kodi log, search for lines starting with `librespot:` — that is librespot’s stderr and will show the actual reason (e.g. zeroconf/discovery bind failure, wrong ALSA device, PulseAudio not available).

- **CoreELEC / AM6B+ (and similar):**
  - Prefer the **ALSA** backend in Connect settings (default). PulseAudio RTP often isn’t available on CoreELEC.
  - If using ALSA, set **Connect ALSA device** to the correct device. Default is `hw:2,0`; on your box it might be `hw:0,0` or `hw:1,0`. Run `aplay -L` over SSH to list devices and pick the right one (e.g. the HDMI or analog output you use).
  - Use the system librespot: `opkg install librespot` so the binary matches your architecture (e.g. aarch64). If you use a bundled binary in the addon’s `bin/` folder, it must be built for your device (e.g. ARM64 for AM6B+).

## Credits

- Based on [plugin.audio.spotify](https://github.com/glk1001/plugin.audio.spotify) by glk1001.
- Design goals inspired by PlexKodiConnect’s integration with Kodi.

## Disclaimer

This product uses the Spotify Web API but is not endorsed, certified or otherwise approved by Spotify. Spotify is the registered trademark of Spotify AB.

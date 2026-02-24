# Spotify Kodi Connect

Unofficial Spotify music addon for Kodi, inspired by the “Kodi Connect” style of PlexKodiConnect. Built on [plugin.audio.spotify](https://github.com/glk1001/plugin.audio.spotify) with improvements for consistent art, metadata, and smoother performance.

## Improvements over plugin.audio.spotify

- **Spotify playlists as real Kodi playlists (like PlexKodiConnect)**  
  Your Spotify playlists are synced to Kodi’s Music playlists folder as `.m3u` files. They show up under **Music → Playlists** as native Kodi playlists, not as addon folders. Play from there like any other playlist. Sync runs automatically after login and every 30 minutes; you can also use **Refresh Kodi playlists** from the addon menu.

- **Consistent art everywhere**  
  Album/artist/playlist art is set for all list types (thumb, poster, fanart, icon). Playlists and song lists no longer show missing art in some views.

- **Correct metadata**  
  Kodi music info is set with proper types (year, duration, tracknumber, discnumber, rating) and labels (albumartist for compilations, genre as string) so tagging and sorting work reliably.

- **Background precache**  
  Library precache runs in a background thread so the main menu appears immediately instead of blocking.

- **Smoother playlist play**  
  “Play playlist” starts the first track right away and queues the rest in the background so playback begins without a long wait.

- **Spotify Connect receiver (LibreSpot)**  
  Optional: enable “Connect receiver” in addon settings to run LibreSpot. Your Kodi device then appears as a speaker in the Spotify app on your iPhone or other devices—tap “Connect to a device” and choose it to stream and control playback from the phone while audio plays on Kodi. Requires the **librespot** binary (e.g. on CoreELEC: `opkg install librespot` or install from your distro). Backends: PulseAudio RTP (Kodi plays the stream) or ALSA (direct to device).

## Requirements

- Kodi 19+ (Python 3)
- Spotify Premium
- Same dependencies as the original addon (script.module.requests, etc.)

## Installation

1. Install this addon (e.g. zip or copy into the addons folder).
2. Configure and authenticate once (same flow as the original Spotify addon).
3. Use **Music → Add-ons → Spotify Kodi Connect** (or your skin’s equivalent).

You can run this addon alongside the original `plugin.audio.spotify`; it uses a different addon id and proxy port (52309) and its own auth/cache.

## Credits

- Based on [plugin.audio.spotify](https://github.com/glk1001/plugin.audio.spotify) by glk1001.
- Design goals inspired by PlexKodiConnect’s integration with Kodi.

## Disclaimer

This product uses the Spotify Web API but is not endorsed, certified or otherwise approved by Spotify. Spotify is the registered trademark of Spotify AB.

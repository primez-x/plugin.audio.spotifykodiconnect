# SpotifyKodiConnect

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

- **Additional song info**  
  The “Album description / Artist biography” area on the music OSD (skin label; e.g. Arctic Fuse 3) is filled from Spotify data only (release date, genre, etc.). No external scrapers or augmentation.

- **Performance**  
  Saved-track and followed-artist lookups run in parallel with track loading. Playlist “Play” queues the rest in a background thread; precache runs in background threads.

## Metadata: what comes from Spotify

- **Tracks:** title, artist(s), album, duration, year, track/disc number, genre, art (thumb/album art), popularity (mapped to rating).
- **Albums:** name, artists, release date, art (from the track/playlist/album API responses).
- **Artists:** name (from track/album responses).  
- The “Album description / Artist biography” OSD area is filled only from Spotify-derived data (e.g. release date, genre). No external scrapers (Last.fm, MusicBrainz, etc.) or content augmentation.

## Requirements

- Kodi 19+ (Python 3)
- Spotify Premium
- Same dependencies as the original addon (script.module.requests, etc.)

## Installation

1. Install this addon (e.g. zip or copy into the addons folder).
2. Configure and authenticate once (same flow as the original Spotify addon).
3. Use **Music → Add-ons → SpotifyKodiConnect** (or your skin’s equivalent).

You can run this addon alongside the original `plugin.audio.spotify`; it uses a different addon id and proxy port (52309) and its own auth/cache.

## Credits

- Based on [plugin.audio.spotify](https://github.com/glk1001/plugin.audio.spotify) by glk1001.
- Design goals inspired by PlexKodiConnect’s integration with Kodi.

## Disclaimer

This product uses the Spotify Web API but is not endorsed, certified or otherwise approved by Spotify. Spotify is the registered trademark of Spotify AB.

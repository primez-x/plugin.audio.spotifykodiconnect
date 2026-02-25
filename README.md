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
  The “Album description / Artist biography” area on the music OSD (skin label; e.g. Arctic Fuse 3) is filled with content from Spotify by default (release date, label, genre, etc.). Enable **Enable content lookup** and choose **Content provider** (**Last.fm** or **MusicBrainz (Wikipedia)**) to fetch richer biographies and album descriptions. With **Last.fm** selected, artist images from Last.fm are also set so the skin’s **Artist slideshow** can show artist photos instead of only album art.

- **Performance**  
  Extra song info uses **streaming enrichment**: the list appears immediately with basic data; a background thread then fetches album (label, copyright) and artist (genres, followers) data for all items in parallel and refreshes the container so descriptions fill in without blocking the UI. The setting “Fetch extra song info” can disable this. Saved-track and followed-artist lookups run in parallel with track loading. Playlist “Play” queues the rest in a background thread; precache also runs in background threads.

## Metadata: what comes from Spotify

- **Tracks:** title, artist(s), album, duration, year, track/disc number, genre, art (thumb/album art), popularity (mapped to rating).
- **Albums:** name, artists, release date, art, label, copyrights (after a batch fetch when building lists).
- **Artists:** name, genres, follower count (after a batch fetch when building lists).  
- **Not in Spotify’s API:** artist biography, album liner notes or long description. The heading “Album description / Artist biography” on the OSD is a fixed skin label; the addon fills the content (Spotify summary or, when content lookup is enabled, Last.fm/MusicBrainz text).

### Optional: biography, album descriptions, and artist images (Last.fm or MusicBrainz)

Turn on **Enable content lookup** in addon settings, then choose **Content provider**:

- **Last.fm** – Artist biography, album description, and artist image from Last.fm. The artist image is set as `artist.fanart` so skins (e.g. Arctic Fuse 3’s “Artist slideshow”) can show it instead of reusing album art. Requires a free API key from [last.fm/api/account/create](https://www.last.fm/api/account/create).
- **MusicBrainz (Wikipedia)** – Artist biography via MusicBrainz + Wikipedia (no API key). Album description falls back to the Spotify summary. The Wikipedia fetch uses the same MediaWiki API as the **script.wikipedia** addon (intro extract); we do not add it as a dependency so this addon stays lightweight and works headlessly in the background.

### Kodi scrapers and Universal scrapers (metadata.album.universal / metadata.artists.universal)

Kodi’s **Generic Artist Scraper**, **Generic Album Scraper**, and the **Universal Album/Artist Scraper** addons (`metadata.album.universal`, `metadata.artists.universal`) are invoked only by Kodi when scanning or querying **library** content. They are not callable from other addons: there is no public API to pass (artist name, album name) and get back biography or description. So they **cannot** be used to fill biography/description for Spotify plugin items. For plugin-only content, this addon fetches metadata itself using Last.fm or MusicBrainz + Wikipedia as above.

For pure Spotify plugin use, the addon provides:

1. **Correct minimum metadata** – song title, artist, album, year, duration, track/disc number, genre, etc. – mapped to Kodi’s music info types so tagging and sorting work reliably.
2. **Optional external metadata** – Enable **Enable content lookup** and choose **Content provider** (**Last.fm** or **MusicBrainz (Wikipedia)**) for biography, album description, and (with Last.fm) artist images for the skin’s artist slideshow.

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

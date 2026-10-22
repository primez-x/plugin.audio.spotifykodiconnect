# SpotifyKodiConnect

Primez maintains SpotifyKodiConnect, a Kodi music add-on fork of [plugin.audio.spotify](https://github.com/glk1001/plugin.audio.spotify). The add-on uses Spotify data for browsing and a background service for audio streams and token renewal.

Spotify supplies the album and artist details shown by the add-on. The add-on does not add data from external music scrapers.

## Install

1. Install the [Primez repository ZIP](https://primez-x.github.io/kodi.addons/repository.primez.addons/repository.primez.addons-1.0.0.zip) in Kodi.
2. Install SpotifyKodiConnect from the Primez repository.
3. Open **Music → Add-ons → SpotifyKodiConnect**.
4. Browse **My Music**, **Explore**, or **Search**.

For central repository details, see the [Primez Kodi add-ons guide](https://github.com/primez-x/kodi.addons/blob/main/README.md).

## First setup and pairing

To start or repeat pairing, select **Authenticate** from the add-on root menu. Use another Spotify app on the same network. Open **Connect to a device**, choose **Kodi-Spotty**, then confirm the prompt in Kodi.

The pairing screen can be cancelled. The add-on waits up to three minutes for pairing credentials. If a browse reports that Spotify is connecting, retry after the background service connects.

## Requirements

- The add-on metadata lists Spotify Premium as required.
- `addon.xml` declares `xbmc.python` 3.0.0 and `xbmc.addon` 18.9.701.
- Declared modules: `script.module.certifi` 2023.5.7, `script.module.chardet` 5.1.0, `script.module.idna` 3.4, `script.module.requests` 2.31.0, and `script.module.urllib3` 1.26.16.
- The manifest declares platform `all`. The manifest does not name a minimum Kodi release or a tested platform list.

## Features and settings

- Browse Spotify library items, playlists, Explore categories, and search results. Large collections load more pages in the background without visible **Next page** entries.
- Artwork and Kodi music metadata appear across listing types. Playlist playback starts with the first track while the add-on queues remaining tracks in the background.
- The background service refreshes Spotify tokens and serves audio streams through a local HTTP service.

## Spotify metadata

- Tracks use Spotify titles, artists, albums, duration, year, track and disc numbers, genre, and popularity mapped to Kodi rating.
- The song OSD album description uses Spotify release date and genre. Full album responses can also supply label and copyright text.
- Artist OSD details show Spotify genres and follower counts. This add-on does not show artist biographies.

Open **Settings** to choose:

- **Playback:** normalization defaults to Auto; bitrate defaults to 320 kbps; Spotify autoplay defaults off; next-track prebuffer defaults on.
- **Prebuffer:** prepare the next audio stream near the current track's end. Disable prebuffer to skip that early preparation.
- **Library:** background library precache defaults off. When enabled, the add-on preloads a bounded set while idle and stops when browsing or audio playback starts.
- **Debug:** detailed logging and listing-time logs both default off.
- **Cache:** listings use a local SQLite-backed cache. Choose **Clear cache** from the root menu to remove cached data.

## Source layout

- `plugin.py`: Kodi browsing and playback entry point.
- `service.py`: background service entry point.
- `resources/lib/plugin_content.py`: Spotify listings, metadata, paging, and cache use.
- `resources/lib/main_service.py`: token renewal, stream service, playback monitoring, and prebuffering.
- `resources/lib/spotty_auth.py`: cancellable Spotify device pairing.
- `resources/settings.xml`: user settings.
- `resources/lib/deps/`: bundled Spotipy, Bottle, and SimpleCache modules.
- `test_*.py`: root-level test modules.

## Tests

Run each configured command from the add-on root:

```sh
python3 -m unittest test_background_threads
python3 -m unittest test_bottle_manager
python3 -m unittest test_cache_checksums
python3 -m unittest test_http_spotty_audio_streamer
python3 -m unittest test_listing_timing
python3 -m unittest test_main_service_osd
python3 -m unittest test_play_queue
python3 -m unittest test_playlist_fastpath
python3 -m unittest test_plugin_auth
python3 -m unittest test_simplecache
python3 -m unittest test_spotipy_retry
python3 -m unittest test_spotty_audio_streamer
python3 -m unittest test_spotty_auth
python3 -m unittest test_stream_seams
```

These commands match `.primez-publish.json`.

## Maintenance

- Primez publication tracks the `master` branch.
- Enable the local publish hook with `git config core.hooksPath .githooks`.
- Changes to shipped behavior require an `addon.xml` version step and a top `<news>` entry. README-only changes need no version bump.
- Follow the [Primez versioning rules](https://github.com/primez-x/kodi.addons/blob/main/VERSIONING.md) and the [central repository guide](https://github.com/primez-x/kodi.addons/blob/main/README.md).
- Report add-on issues in the [Primez SpotifyKodiConnect repository](https://github.com/primez-x/plugin.audio.spotifykodiconnect/issues).

## Spotty ARM helper

The ARMv7 hard-float helper uses Spotty 1.3.1 and librespot 0.4.2 with connection fallback.
The patch preserves the original login, token, and PCM paths.
Other platform binaries retain their existing builds and ABIs.
The shipped ARM helper SHA-256 is `8ed6bef8e81229d81e7cbe7559bfc07f4e29c4adc87475fe03db15b6f65b0c33`.

Rebuild with Docker and Rust 1.71.0:

```sh
rustup toolchain install 1.71.0 --profile minimal --target armv7-unknown-linux-musleabihf
spotty_source=$(mktemp -d)
git clone --no-checkout https://github.com/michaelherger/librespot "$spotty_source"
git -C "$spotty_source" checkout --detach d19782444e3c50cba6b0d47fb881a0de6774ce4c
git -C "$spotty_source" config core.abbrev 7
touch "$spotty_source/src/client_id.txt"
git -C "$spotty_source" apply "$PWD/docs/spotty-ap-fallback.patch"
sh docs/build-spotty.sh "$spotty_source" /tmp/spotty-muslhf
```

The build date changes the binary hash across days.

## Credits and license

- Based on [plugin.audio.spotify](https://github.com/glk1001/plugin.audio.spotify) by glk1001.
- Design goals draw inspiration from PlexKodiConnect.
- The source tree includes the [GNU GPL version 3 license](LICENSE).

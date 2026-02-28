# Spotify Web API – Complete Data Reference

Verbose list of all data available from the [Spotify Web API](https://developer.spotify.com/documentation/web-api/reference). Source: [Spotify for Developers](https://developer.spotify.com/documentation/web-api/reference). Some fields are **deprecated** but still returned.

---

## Common / Shared Objects

### ImageObject
Used by Album, Artist, User, Playlist, etc.

| Field   | Type    | Description |
|--------|---------|-------------|
| `url`  | string  | Source URL of the image |
| `height` | integer \| null | Height in pixels |
| `width`  | integer \| null | Width in pixels |

### ExternalUrlObject
| Field     | Type   | Description |
|----------|--------|-------------|
| `spotify` | string | Spotify URL for the object |

### RestrictionObject
| Field   | Type   | Description |
|--------|--------|-------------|
| `reason` | string | `"market"`, `"product"`, `"explicit"`, or future values |

---

## Track Object (full)

From **GET /tracks/{id}** (and embedded in playlists, albums, search).

| Field               | Type    | Description |
|---------------------|---------|-------------|
| `album`             | Album (simplified) | Album on which the track appears |
| `artists`           | array of SimplifiedArtistObject | Performing artists |
| `available_markets` | array of strings | **(Deprecated)** ISO 3166-1 alpha-2 country codes |
| `disc_number`       | integer | Disc number (usually 1) |
| `duration_ms`       | integer | Length in milliseconds |
| `explicit`          | boolean | Whether the track has explicit lyrics |
| `external_ids`      | object | **(Deprecated)** External IDs: `isrc`, `ean`, `upc` |
| `external_urls`     | ExternalUrlObject | Spotify URL, etc. |
| `href`              | string | Web API endpoint for the track |
| `id`                | string | Spotify ID |
| `is_playable`       | boolean | When track relinking is applied: whether playable in market |
| `linked_from`       | object | **(Deprecated)** When relinking: info about originally requested track |
| `restrictions`      | RestrictionObject | When content is restricted |
| `name`              | string | Track name |
| `popularity`        | integer | **(Deprecated)** 0–100 |
| `preview_url`       | string \| null | **(Deprecated)** 30-second MP3 preview URL |
| `track_number`      | integer | Track number on disc |
| `type`              | string | `"track"` |
| `uri`               | string | Spotify URI |
| `is_local`          | boolean | Whether the track is from a local file |

**Nested in track: `album` (simplified)**  
`album_type`, `total_tracks`, `available_markets`, `external_urls`, `href`, `id`, `images`, `name`, `release_date`, `release_date_precision`, `restrictions`, `type`, `uri`, `artists` (array of simplified artists).

**Nested: each `artists[]` (SimplifiedArtistObject)**  
`external_urls`, `href`, `id`, `name`, `type` (`"artist"`), `uri`.

---

## Simplified Track Object

As returned in album tracks, playlist items, search, etc. Subset of full Track: often no `album` (or simplified), and fewer fields.

| Field               | Type    | Description |
|---------------------|---------|-------------|
| `artists`           | array of SimplifiedArtistObject | Performing artists |
| `available_markets` | array of strings | **(Deprecated)** |
| `disc_number`       | integer | Disc number |
| `duration_ms`       | integer | Length in milliseconds |
| `explicit`          | boolean | Explicit lyrics |
| `external_urls`     | ExternalUrlObject | |
| `href`              | string | |
| `id`                | string | Spotify ID |
| `is_playable`       | boolean | When relinking applied |
| `linked_from`       | object | **(Deprecated)** Original track when relinked |
| `restrictions`      | RestrictionObject | |
| `name`              | string | Track name |
| `preview_url`       | string \| null | **(Deprecated)** 30s preview |
| `track_number`      | integer | |
| `type`              | string | `"track"` |
| `uri`               | string | |
| `is_local`          | boolean | Local file |

When track is inside a **playlist item**, the item wraps it as `track` and adds:

| Field      | Type   | Description |
|-----------|--------|-------------|
| `added_at` | string (date-time) | When the track/episode was added |
| `added_by` | User (simplified) | User who added it |
| `is_local` | boolean | |

---

## Artist Object (full)

From **GET /artists/{id}**.

| Field           | Type   | Description |
|-----------------|--------|-------------|
| `external_urls` | ExternalUrlObject | |
| `followers`     | object | **(Deprecated)** `href` (null), `total` (integer) |
| `genres`        | array of strings | **(Deprecated)** e.g. `["Prog rock","Grunge"]` |
| `href`          | string | Web API endpoint |
| `id`            | string | Spotify ID |
| `images`        | array of ImageObject | **One** artist image in various sizes (widest first; same as album art). Not multiple different photos. |
| `name`          | string | Artist name |
| `popularity`    | integer | **(Deprecated)** 0–100 |
| `type`          | string | `"artist"` |
| `uri`           | string | Spotify URI |

**API note:** Per [Get Artist](https://developer.spotify.com/documentation/web-api/reference/get-an-artist), `images` is “Images of the artist **in various sizes, widest first**”—i.e. the same image at different resolutions (e.g. 640×640, 300×300, 64×64), not multiple distinct photos.

**Addon usage:** The plugin uses the **largest** (first) image everywhere: track list items get `artist.fanart`; artist list items get `thumb`/`poster`/`fanart`/`icon`; Music OSD and list backgrounds use that art. GET /artists/ (batch) when preparing tracks; artist browse uses full artist objects from the API (which include `images`).

### SimplifiedArtistObject
Subset used inside Track, Album, etc.: `external_urls`, `href`, `id`, `name`, `type`, `uri` (no `followers`, `genres`, `images`, `popularity`).

---

## Album Object (full)

From **GET /albums/{id}**.

| Field                 | Type   | Description |
|-----------------------|--------|-------------|
| `album_type`          | string | `"album"`, `"single"`, `"compilation"` |
| `total_tracks`        | integer | Number of tracks |
| `available_markets`   | array of strings | **(Deprecated)** ISO 3166-1 alpha-2 |
| `external_urls`       | ExternalUrlObject | |
| `href`                | string | |
| `id`                  | string | Spotify ID |
| `images`              | array of ImageObject | Cover art, various sizes |
| `name`                | string | Album name |
| `release_date`        | string | e.g. `"1981-12"` |
| `release_date_precision` | string | `"year"`, `"month"`, `"day"` |
| `restrictions`        | RestrictionObject | |
| `type`                | string | `"album"` |
| `uri`                 | string | |
| `artists`             | array of SimplifiedArtistObject | Album artists |
| `tracks`              | PagingObject of SimplifiedTrackObject | Tracks (with `href`, `limit`, `next`, `offset`, `previous`, `total`, `items`) |
| `copyrights`          | array of CopyrightObject | `text`, `type` (e.g. `"C"`, `"P"`) |
| `external_ids`        | object | **(Deprecated)** `isrc`, `ean`, `upc` |
| `genres`              | array of strings | **(Deprecated)** Documented as always empty |
| `label`               | string | **(Deprecated)** Label name |
| `popularity`          | integer | **(Deprecated)** 0–100 |

### SimplifiedAlbumObject
Subset inside Track, etc.: e.g. `album_type`, `total_tracks`, `available_markets`, `external_urls`, `href`, `id`, `images`, `name`, `release_date`, `release_date_precision`, `restrictions`, `type`, `uri`, `artists`. No `tracks`, `copyrights`, `label`, `genres`, `popularity`.

---

## Playlist Object

From **GET /playlists/{playlist_id}** (optional `fields` query).

| Field            | Type   | Description |
|------------------|--------|-------------|
| `collaborative`  | boolean | Others can edit |
| `description`   | string \| null | Playlist description (modified/verified only) |
| `external_urls`  | ExternalUrlObject | |
| `href`          | string | |
| `id`            | string | Spotify ID |
| `images`        | array of ImageObject | Up to 3, may be empty; URLs temporary |
| `name`          | string | Playlist name |
| `owner`         | User (simplified) | Owner: `display_name`, `external_urls`, `href`, `id`, `type`, `uri` |
| `public`        | boolean \| null | Public/private or N/A |
| `snapshot_id`   | string | Version id for the playlist |
| `tracks`        | object | Playlist items: `href`, `limit`, `next`, `offset`, `previous`, `total`, `items` (PlaylistTrackObject[]) |

**PlaylistTrackObject** (each item in `tracks.items`):  
`added_at`, `added_by` (User), `is_local`, `track` (Track \| Episode \| null), plus `primary_color`, `video_thumbnail` (optional).

---

## User Object

From **GET /users/{user_id}** or **GET /me** (current user).

| Field           | Type   | Description |
|-----------------|--------|-------------|
| `display_name`  | string \| null | Profile display name |
| `external_urls` | ExternalUrlObject | |
| `followers`     | object | **(Deprecated)** `href` (null), `total` |
| `href`          | string | |
| `id`            | string | Spotify user ID |
| `images`        | array of ImageObject | Profile image(s) |
| `type`          | string | `"user"` |
| `uri`           | string | |

**Current user (GET /me)** can also include: `country`, `email`, `product` (e.g. `"premium"`), `explicit_content`, etc., depending on scopes.

---

## Category Object

From **GET /browse/categories/{id}** (browse).

| Field           | Type   | Description |
|-----------------|--------|-------------|
| `href`          | string | Link to category |
| `icons`         | array of ImageObject | Category icons |
| `id`            | string | Category ID |
| `name`          | string | Category name |

---

## PagingObject (generic)

Used for paginated lists (tracks, playlists, albums, etc.).

| Field     | Type   | Description |
|----------|--------|-------------|
| `href`   | string | Full request URL |
| `limit`  | integer | Page size |
| `next`   | string \| null | URL of next page |
| `offset` | integer | Offset |
| `previous` | string \| null | URL of previous page |
| `total`  | integer | Total items |
| `items`  | array  | This page’s items |

---

## Search

**GET /search** with `q` and `type` (e.g. `track`, `album`, `artist`, `playlist`, `show`, `episode`, `audiobook`).

Response: one key per requested type, each a **PagingObject** of that type (e.g. `tracks`, `albums`, `artists`, `playlists`, `shows`, `episodes`, `audiobooks`).

---

## Other Endpoints / Data (summary)

- **Shows** – Podcast shows: id, name, description, media type, publisher, images, episodes (paged), etc.
- **Episodes** – Single episode: name, description, duration_ms, release_date, images, show, audio_preview_url, etc.
- **Audiobooks** – Audiobook metadata and **chapters** (separate endpoint).
- **Library** – Current user’s saved tracks, albums, episodes, shows; follow artists/users/playlists; create/follow playlists; add/remove from playlists.
- **Playback** – Currently playing, queue, transfer, start/pause/seek/skip (control playback).
- **Browse** – Featured playlists, new releases, categories, category playlists (all with market).
- **Follow** – Follow artists/users, follow playlists, check follow state.
- **Personalization** – Top tracks, top artists (with time_range).

---

## Deprecation notes (API docs)

Many fields are marked **deprecated** but still present:

- **Track:** `available_markets`, `external_ids`, `linked_from`, `popularity`, `preview_url`
- **Artist:** `followers`, `genres`, `popularity`
- **Album:** `available_markets`, `external_ids`, `genres`, `label`, `popularity`
- **User:** `followers`

Use them only with the understanding they may be removed in a future API version.

---

## Quick reference: what the addon typically uses

From Spotify responses the addon commonly uses:

- **Track:** `id`, `name`, `duration_ms`, `track_number`, `disc_number`, `explicit`, `uri`, `artists` (id, name), `album` (id, name, images, release_date, album_type), `popularity` (for rating)
- **Album:** `id`, `name`, `images`, `release_date`, `artists`, `album_type`, (when full) `label`, `copyrights`, `tracks`
- **Artist:** `id`, `name`, (when full) `genres`, `followers`, `images`
- **Playlist:** `id`, `name`, `owner`, `tracks` (total, items with track)
- **User (me):** `id`, `country`, `email` (or id for display)

All of the above are part of the full data available from the Spotify Web API as described in this reference.

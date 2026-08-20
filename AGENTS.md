# Repository Guidelines

## Project Structure & Module Organization
This repository is a Kodi audio add-on named `plugin.audio.spotifykodiconnect`. `addon.xml` is the manifest, `plugin.py` is the plugin entry point, and `service.py` starts the background service. Runtime code lives in `resources/lib/`, with vendored dependencies under `resources/lib/deps/`. Tests are root-level `test_*.py` files.

## Build, Test, and Development Commands
- `python -m compileall plugin.py service.py resources/lib`: checks Python syntax without importing Kodi runtime modules.
- `python test_headers.py`: runs the lightweight header checks used in this repo.
- `python -m black --check .`: checks formatting where practical; avoid reformatting vendored dependency files unless the task explicitly requires it.

## Coding Style & Naming Conventions
Use Python 3, 4-space indentation, `snake_case` for functions and variables, `PascalCase` for classes, and `UPPER_CASE` for constants. Keep Kodi-specific calls isolated from reusable helpers where practical. Match existing metadata formatting in `addon.xml` and keep source URLs, provider metadata, and `news` entries synchronized with release bumps.

## UI & UX Guidelines
Never expose manual "Next page" items, buttons, or list entries in SpotifyKodiConnect browsing flows. If Spotify API pagination is needed, hide it behind dynamic paging, incremental loading, background continuation, or another automatic interaction model. Visible pagination controls are not acceptable UX for this project.

## Testing Guidelines
For Python changes, run `compileall` and the focused root test that covers the touched behavior. For metadata-only changes, parse `addon.xml` and run `git diff --check`. Kodi playback changes should be smoke-tested on CoreELEC when practical, especially startup, queueing, and artwork paths.

## Commit & Pull Request Guidelines
Use short imperative commit subjects. For Primez repository publishing, any commit pushed to the tracked `master` branch must bump the root `addon.xml` version in the same commit. Kodi auto-update consumes the generated repository version, not the Git SHA, and the central `kodi.addons` publish guard rejects webhook publishes whose source version does not increase.

Before a CoreELEC install or publish, compare the target installation's version and every source-tracked file with the candidate source. If device-only deltas exist, preserve an out-of-repo whole-add-on rollback, layer only the requested change, verify unrelated tracked files remain unchanged, and block publication until every delta is either incorporated into source or intentionally removed from the target.

## Security & Configuration Tips
Do not commit Spotify credentials, Kodi profile data, cache files, logs with tokens, generated ZIPs, or `.pyc` files.

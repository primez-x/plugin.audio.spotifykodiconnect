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
Use short imperative commit subjects.

Before a CoreELEC install or publish, compare the target installation's version and every source-tracked file with the candidate source. If device-only deltas exist, preserve an out-of-repo whole-add-on rollback, layer only the requested change, verify unrelated tracked files remain unchanged, and block publication until every delta is either incorporated into source or intentionally removed from the target.

## Security & Configuration Tips
Do not commit Spotify credentials, Kodi profile data, cache files, logs with tokens, generated ZIPs, or `.pyc` files.

## Primez Publish Rules

The tracked branch is `master`: every push to it is published to the Primez Kodi repository, and every push is exactly one release. Kodi auto-update follows the repository version, not the Git SHA.

Each push raises the root `addon.xml` version by exactly one step from the branch tip — raise one component by 1 and reset the ones after it — and adds a `<news>` entry on top whose first line names the new version (match the existing format). Which component to raise is decided by `VERSIONING.md` in `primez-x/kodi.addons`: **major** when users or other add-ons must act or something breaks, **minor** for new or changed user-visible behaviour, **patch** for fixes, performance, tooling, tests and docs (take the highest level that applies). A push may hold several commits; only its tip version counts. Adopting an upstream version is the only allowed jump and needs a `Version-Jump: <reason>` line in the tip commit message.

`.githooks/pre-push` enforces this before the push leaves the machine (version above the branch tip, news entry, and the `tests` in `.primez-publish.json` passing on the pushed commit); enable it with `git config core.hooksPath .githooks` (Claude Code sessions do this automatically). `.github/workflows/publish-check.yml` runs the same check on GitHub, and the `kodi.addons` publish refuses commits that fail it. `.githooks/publish_check.py` is a copy of `primez-x/kodi.addons` `tools/publish_check.py`; change it there and re-copy it. Keep `.primez-publish.json` tests in sync with how this repository's tests are run.

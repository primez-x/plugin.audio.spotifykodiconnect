#!/bin/sh
set -eu

# Build the ARMv7 hard-float helper from a prepared, patched source checkout.
SPOTTY_SOURCE_DIR=$(cd "$1" && pwd)
SPOTTY_BUILD_DIR=$(mktemp -d "${TMPDIR:-/tmp}/spotifykodiconnect-spotty.XXXXXX")
trap 'rm -rf "$SPOTTY_BUILD_DIR"' EXIT
SPOTTY_IMAGE=ghcr.io/cross-rs/armv7-unknown-linux-musleabihf@sha256:b4f481f30ceca34730a5723f717396d762d94f6eddd7e1cf7c7862d1377ff98c

docker run --rm -u "$(id -u):$(id -g)" \
  -e HOME=/tmp -e RUSTUP_HOME=/rustup -e CARGO_HOME=/cargo \
  -e SOURCE_DATE_EPOCH=1792000000 -e CARGO_TARGET_DIR=/target \
  -e PATH=/rustup/toolchains/1.71.0-x86_64-unknown-linux-gnu/bin:/usr/local/bin:/usr/bin:/bin \
  -v "$HOME/.rustup:/rustup:ro" -v "$HOME/.cargo:/cargo" \
  -v "$SPOTTY_BUILD_DIR:/target" -v "$SPOTTY_SOURCE_DIR:/src:ro" -w /src \
  "$SPOTTY_IMAGE" cargo build --locked --release --target armv7-unknown-linux-musleabihf
cp "$SPOTTY_BUILD_DIR/armv7-unknown-linux-musleabihf/release/spotty" "$2"

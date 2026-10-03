#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
VERSION=1.37.0
SHA=60a420ad7085eb616cb6e2bdf0a7206d68ff3d37fb5a956dc44242eb2f79b66b
ARCHIVE="$ROOT/vendor/aria2-$VERSION.tar.xz"
SOURCE="$ROOT/vendor/aria2-$VERSION"
mkdir -p "$ROOT/vendor" "$ROOT/bin" "$ROOT/logs"
if [[ ! -f "$ARCHIVE" ]]; then
  curl -fL --retry 3 "https://github.com/aria2/aria2/releases/download/release-$VERSION/aria2-$VERSION.tar.xz" -o "$ARCHIVE.tmp"
  mv "$ARCHIVE.tmp" "$ARCHIVE"
fi
printf '%s  %s\n' "$SHA" "$ARCHIVE" | sha256sum -c -
if [[ ! -d "$SOURCE" ]]; then tar -xf "$ARCHIVE" -C "$ROOT/vendor"; fi
python3 "$ROOT/patch_aria2.py" "$SOURCE"
cd "$SOURCE"
./configure --prefix="$ROOT" --with-openssl --without-gnutls \
  --without-libxml2 --without-libexpat --without-libssh2 \
  --disable-bittorrent --disable-metalink --disable-nls \
  > "$ROOT/logs/aria2-configure.log" 2>&1
make -j "${BUILD_JOBS:-8}" > "$ROOT/logs/aria2-build.log" 2>&1
# Stage replacement so a failed build never destroys the previous binary.
install -m 755 src/aria2c "$ROOT/bin/aria2c.new"
if command -v strip >/dev/null 2>&1; then strip --strip-unneeded "$ROOT/bin/aria2c.new"; fi
mv "$ROOT/bin/aria2c.new" "$ROOT/bin/aria2c"
cp COPYING "$ROOT/vendor/ARIA2_COPYING"
python3 - "$ROOT" "$SHA" <<'PY'
import hashlib, json, pathlib, platform, sys
root = pathlib.Path(sys.argv[1])
binary = root / 'bin/aria2c'
metadata = dict(upstream='aria2 1.37.0', source_sha256=sys.argv[2],
                binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                max_connections=512, low_speed_policy='upstream-native',
                system=platform.platform(), machine=platform.machine())
(root / 'bin/build-info.json').write_text(json.dumps(metadata, indent=2) + '\n')
PY
"$ROOT/bin/aria2c" --help=max-connection-per-server

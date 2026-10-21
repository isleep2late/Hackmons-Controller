#!/bin/sh
# Build libSDL3.so.0 for the Linux bundle inside the manylinux_2_28 image (AlmaLinux 8, glibc 2.28),
# so the bundle runs on any x86-64 distribution from about 2019 on (Ubuntu 20.04, Debian 11, ...),
# not only on ones as new as the build machine. Joystick/HID only, console build (no video/audio).
#
#   scripts/build_sdl3_linux.sh [OUT_DIR]       -> OUT_DIR/libSDL3.so.0 (default: build/sdl3)
#
# Needs docker (or podman: DOCKER=podman). SDL_VERSION overrides the SDL release.
set -eu
SDL_VERSION=${SDL_VERSION:-3.4.16}
IMAGE=${IMAGE:-quay.io/pypa/manylinux_2_28_x86_64}
DOCKER=${DOCKER:-docker}
OUT=$(mkdir -p "${1:-build/sdl3}" && cd "${1:-build/sdl3}" && pwd)

"$DOCKER" run --rm -v "$OUT:/out" -e SDL_VERSION="$SDL_VERSION" \
  -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" "$IMAGE" sh -euc '
    dnf install -y -q systemd-devel dbus-devel libusbx-devel ninja-build >/dev/null
    cd /tmp
    curl -sSL -o SDL3.tar.gz "https://github.com/libsdl-org/SDL/releases/download/release-$SDL_VERSION/SDL3-$SDL_VERSION.tar.gz"
    tar xzf SDL3.tar.gz
    cmake -S "SDL3-$SDL_VERSION" -B sdl-build -G Ninja -DCMAKE_BUILD_TYPE=Release \
      -DSDL_SHARED=ON -DSDL_STATIC=OFF -DSDL_TESTS=OFF -DSDL_EXAMPLES=OFF -DSDL_INSTALL_TESTS=OFF \
      -DSDL_AUDIO=OFF -DSDL_VIDEO=OFF -DSDL_RENDER=OFF -DSDL_GPU=OFF -DSDL_CAMERA=OFF \
      -DSDL_DIALOG=OFF -DSDL_TRAY=OFF -DSDL_UNIX_CONSOLE_BUILD=ON \
      -DSDL_HIDAPI=ON -DSDL_HIDAPI_LIBUSB=ON -DSDL_JOYSTICK=ON -DSDL_HAPTIC=ON -DSDL_SENSOR=ON -DSDL_POWER=ON
    cmake --build sdl-build -j"$(nproc)"
    cp -L sdl-build/libSDL3.so.0 /out/libSDL3.so.0
    chown "$HOST_UID:$HOST_GID" /out/libSDL3.so.0
  '
need=$(objdump -T "$OUT/libSDL3.so.0" | grep -o 'GLIBC_[0-9.]*' | sort -V | tail -n1)
echo "built $OUT/libSDL3.so.0 (SDL $SDL_VERSION, needs $need)"

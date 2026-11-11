#!/bin/sh
# Build the iDBox LCD renderer.
#
#   tools/build.sh            # rebuild font_aa.h + fbdash (arm/v7 static)
#   tools/build.sh host       # same but for this machine (testing only)
#
# The panel has no freetype, so the antialiased face is baked at build time into
# font_aa.h (8-bit alpha, 95 glyphs, sizes 11/13/16/22 px).
set -e
cd "$(dirname "$0")/.."

FONT=${FONT:-/usr/share/fonts/TTF/JetBrainsMonoNerdFont-Regular.ttf}
NUMFONT=${NUMFONT:-../projetor/assets/fonts/Manrope-800.ttf}
SIZES=${SIZES:-"11 13 16 22 40"}

if [ ! -x tools/bake_font ] || [ tools/bake_font.c -nt tools/bake_font ]; then
    gcc -O2 -o tools/bake_font tools/bake_font.c $(pkg-config --cflags --libs freetype2)
fi
tools/bake_font "$FONT" $SIZES > font_aa.h
tools/bake_font --prefix NUM_ --fixed "$NUMFONT" 40 22 > num_aa.h
echo "font_aa.h: $(wc -c < font_aa.h) bytes, num_aa.h: $(wc -c < num_aa.h) bytes (Manrope 800, tabular)"

if [ "$1" = "host" ]; then
    gcc -O2 -o fbdash.host fbdash.c -lm
    echo "built fbdash.host"
    exit 0
fi

docker run --rm --platform linux/arm/v7 -v "$PWD":/src alpine:3.20 sh -c \
    'apk add --no-cache gcc musl-dev linux-headers >/dev/null 2>&1 && gcc -O2 -static -o /src/fbdash /src/fbdash.c'
file fbdash | sed 's/^/  /'
echo "deploy: scp fbdash root@192.168.5.129:/mnt/fs_user/fbdash && /etc/init.d/fbdash restart"

#!/bin/bash
# Launcher for the Linux version of RimWorld
# Modified: respect existing LANG/LC_ALL instead of forcing LC_ALL=C

# cd into the directory containing this script
SCRIPT=$(readlink -f "$0")
DIR=$(dirname "$SCRIPT")
cd "$DIR"

# Getting game executable name.
GAMEFILE="RimWorldLinux"

# Puts the game executable as... executable if not the case.
if [ ! -x "$GAMEFILE" ]; then
    chmod +x "$GAMEFILE"
fi

# Only fall back to C locale if the user has not set any locale.
# The original script forced LC_ALL=C, which breaks CJK font rendering
# for players with LANG=zh_CN.UTF-8, ja_JP.UTF-8, ko_KR.UTF-8, etc.
if [ -z "$LANG" ] && [ -z "$LC_ALL" ]; then
    LC_ALL=C ./"$GAMEFILE" "$@"
else
    ./"$GAMEFILE" "$@"
fi

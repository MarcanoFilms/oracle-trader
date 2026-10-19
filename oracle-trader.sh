#!/usr/bin/env bash
# Lanzador de Oracle Trader
cd "$(dirname "$(readlink -f "$0")")"
export ELECTRON_OZONE_PLATFORM_HINT="${ELECTRON_OZONE_PLATFORM_HINT:-auto}"
exec ./node_modules/.bin/electron . "$@"

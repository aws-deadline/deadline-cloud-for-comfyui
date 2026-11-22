#!/bin/sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
set -eu
if [ "$(uname -s)" != "Darwin" ]; then
    echo "The customer installer currently builds on macOS only." >&2
    exit 1
fi
hatch run installer:build-installer "$@"

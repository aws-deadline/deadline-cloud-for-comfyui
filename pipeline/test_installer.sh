#!/bin/sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
hatch run test-installer "$@"

#!/bin/sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
set -eu
hatch run installer:build-installer "$@"

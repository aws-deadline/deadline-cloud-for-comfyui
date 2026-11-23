# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
hatch run installer:build-installer @args
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

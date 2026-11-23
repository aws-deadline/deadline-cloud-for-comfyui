# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
hatch run test-installer @args
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

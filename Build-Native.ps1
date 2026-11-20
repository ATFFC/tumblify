# SPDX-License-Identifier: GPL-3.0-only
$ErrorActionPreference = 'Stop'
$engine = Join-Path $PSScriptRoot 'vendor/box3d'
$native = Join-Path $PSScriptRoot 'native'
$build = Join-Path $PSScriptRoot 'build/native'
& cmake -S $native -B $build "-DFETCHCONTENT_SOURCE_DIR_BOX3D=$engine" -DTUMBLIFY_ENGINE_TESTS=OFF -DBUILD_SHARED_LIBS=OFF -DCMAKE_BUILD_TYPE=Release
if ($LASTEXITCODE -ne 0) { throw 'CMake configure failed.' }
& cmake --build $build --config Release --parallel
if ($LASTEXITCODE -ne 0) { throw 'Native build failed.' }
Write-Host 'Bridge built at native/bin/box3d_blender.dll.'

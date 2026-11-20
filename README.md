# Tumblify

Physics-based object placement for Blender. Drop, stack, fill and emit your meshes with real-time physics, then keep the result as light instances or bake the fall to keyframes. Powered by [Box3D](https://github.com/erincatto/box3d).

This repository is the complete source for the Tumblify add-on as sold on Superhive: the Blender add-on (Python) and the native bridge that wraps Box3D. Ready-to-install builds, documentation and support are on the Superhive product page.

- Version: 1.0
- Blender 5.2, Windows x64

## License

- Tumblify's Python code and native bridge: **GPL-3.0-only**, see [LICENSE](LICENSE).
- Box3D (in `vendor/box3d`): **MIT**, see `vendor/box3d/LICENSE`. The notice shipped with the add-on is in `src/Tumblify/LICENSE_BOX3D.txt`.

The bundled Box3D is based on commit `f36d061ba26998c87cb7dc593a4065aaa03fb7eb` with Tumblify's local Mesh/Height Field contact-query changes. Build from this copy rather than the unmodified upstream revision.

## Layout

| Path | Contents |
| --- | --- |
| `src/Tumblify` | The Blender extension (Python, `blender_manifest.toml`) |
| `native` | C/C++ bridge between Blender and Box3D, CMake project |
| `vendor/box3d` | Box3D source with Tumblify's changes |
| `Build-Native.ps1` | Builds `native/bin/box3d_blender.dll` |

## Building

Windows x64, CMake 3.22+ and Visual Studio C++ Build Tools with the Windows SDK. In an x64 Developer PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\Build-Native.ps1
```

The DLL lands in `native/bin/box3d_blender.dll`. The build script does not touch an installed add-on.

To package the extension, copy the DLL to `src/Tumblify/native/bin/box3d_blender.dll` and run Blender's extension builder:

```powershell
blender --command extension build --source-dir src\Tumblify --output-dir dist
blender --command extension validate dist\tumblify-1.0.0.zip
```

Install the resulting ZIP by dragging it into Blender, or from Edit > Preferences > Get Extensions > Install from Disk.

SHA-256 of the DLL in the distributed 1.0 package (`Tumblify_1.0_Win64_Blender52.zip`):
`f507798c7ee1783c1116fea5fc17dc522ea421d99b984719be190c573789f57e`

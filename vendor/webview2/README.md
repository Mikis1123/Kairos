# WebView2 SDK (map host)

Used by `map_host.py` to host the in-app map in Microsoft Edge WebView2 (Gate 2).

| File | What | SHA-256 |
|---|---|---|
| `Microsoft.Web.WebView2.Core.dll` | managed WebView2 API (.NET Framework 4.x), loaded through pythonnet | `8219a3135436c147f8eb02fb831e99596103eebc18b6d7b794ca93f4c67018a2` |
| `WebView2Loader.dll` | native loader, win-x64. It must sit next to Core.dll | `eb5335edeaa6f2122c044e82aab2351dc8c9b4e0fe5e578c71922fb61be285e7` |

- **Version:** SDK 1.0.3856.49, from NuGet package `Microsoft.Web.WebView2`. These are the copies that pywebview 6.2.1 ships.
- **License:** Microsoft WebView2 SDK license (BSD-style, redistributable): https://www.nuget.org/packages/Microsoft.Web.WebView2/1.0.3856.49/License
- **Runtime:** the browser itself is the WebView2 Evergreen Runtime, which ships with Windows 11. It is not bundled. When it is missing, `map_host.available()` returns False and the map falls back to QtWebEngine.

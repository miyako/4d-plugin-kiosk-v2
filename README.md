![version](https://img.shields.io/badge/version-18%2B-EB8E5F)
![platform](https://img.shields.io/static/v1?label=platform&message=mac-intel%20|%20mac-arm%20|%20win-64&color=blue)
[![license](https://img.shields.io/github/license/miyako/4d-plugin-kiosk-v2)](LICENSE)
![downloads](https://img.shields.io/github/downloads/miyako/4d-plugin-kiosk-v2/total)

# 4d-plugin-kiosk-v2

#### Features

#### macOS

change `NSApplicationPresentationOptions`

* NSApplicationPresentationHideMenuBar
* NSApplicationPresentationHideDock
* NSApplicationPresentationDisableAppleMenu
* NSApplicationPresentationDisableProcessSwitching
* NSApplicationPresentationDisableForceQuit
* NSApplicationPresentationDisableSessionTermination
* NSApplicationPresentationDisableHideApplication

no window is set to full screen; do it manually.

#### Windows

filter certain key combinations

* Ctrl+Esc
* Alt+Tab, Alt+Shift+Tab
* Alt+Esc, Alt+Shift+Esc
* Ctrl+Shift+Esc
* Alt+F4
* Windows (left and right)

Ctrl+Alt+Delete is not filtered

set MDI window parameters

* WS_POPUP

remove MDI window parameters

* WS_CAPTION
* WS_SYSMENU

hide task tray

maximise MDI

### syntax

```4d
If (0=KIOSK Get mode)
	KIOSK SET MODE(1)
Else 
	KIOSK SET MODE(0)
End if 
```

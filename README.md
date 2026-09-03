![version](https://img.shields.io/badge/version-18%2B-EB8E5F)
![platform](https://img.shields.io/static/v1?label=platform&message=mac-intel%20|%20mac-arm%20|%20win-64&color=blue)
[![license](https://img.shields.io/github/license/miyako/4d-plugin-kiosk-v2)](LICENSE)
![downloads](https://img.shields.io/github/downloads/miyako/4d-plugin-kiosk-v2/total)

# 4d-plugin-kiosk-v2

KIOSK locks the current 4D session into a restricted "kiosk" desktop mode, and back again. **On Windows**, it hides the taskbar and system tray, strips the main 4D window's title bar and system menu (raw Win32 window styles, `WS_POPUP`/`WS_CAPTION`/`WS_SYSMENU`), installs a low-level keyboard hook to block common escape shortcuts, and writes a per-user registry policy that disables Task Manager. **On macOS**, it changes the app's `NSApplicationPresentationOptions` to hide the menu bar and Dock and disable app switching, Force Quit, and session termination. The plugin exposes exactly two commands built around a single `Longint` state flag — there's no `Picture`/`Blob`/window-geometry API involved. Neither platform is put into full-screen automatically; you handle window sizing yourself.

| Command | Returns | Purpose |
|---|---|---|
| [KIOSK SET MODE](#kiosk-set-mode) | *(none)* | Turns kiosk mode on or off |
| [KIOSK Get mode](#kiosk-get-mode) | Longint | Returns the current kiosk mode state |

**Platforms:** macOS (Intel & Apple Silicon), Windows 64-bit

---

## Requirements & platform notes

- Both commands take **only** the parameters shown below — there's no optional form of either one.
- **Neither command ever raises a 4D error.** Every underlying OS-level failure path in the plugin is silently ignored; a command call always "succeeds" from 4D's point of view even if the requested OS-level change didn't actually take effect. See [Error handling & troubleshooting](#error-handling--troubleshooting).
- **No window is put into full-screen automatically on either platform.** You need to resize/position your window yourself before or after switching mode.
- **On Windows**, hiding the taskbar only hides the primary taskbar (`Shell_TrayWnd`); on a multi-monitor setup, secondary per-monitor taskbars stay visible.
- **On Windows**, Ctrl+Alt+Delete cannot be intercepted by the plugin's keyboard hook — that key combination is handled by Winlogon's Secure Attention Sequence, below the level any app-installed hook can see. This is why Task Manager is blocked via a registry policy instead (see below), not the hook.
- **On macOS**, no special permission or entitlement is required — `NSApplicationPresentationOptions` is a normal, app-scoped AppKit API with no permission prompt.

---

## KIOSK SET MODE

### Syntax
```4d
KIOSK SET MODE ( mode )
```

| Parameter | Type | Description |
|---|---|---|
| `mode` | Longint | `1` = turn kiosk mode on. `0` = turn kiosk mode off. |
| Result | — | This command does not return a value. |

### Description
`mode` is mandatory. Only `0` and `1` are meaningful — anything else is **not validated**: it's silently accepted into the plugin's internal state, but no OS-level action is taken for it (see the [Error handling](#error-handling--troubleshooting) note on this). Calling the command with the same value as the current mode is a no-op — none of the OS-level steps below repeat.

**Turning mode on:**
- **On Windows:** hides the task tray, installs a low-level keyboard hook that blocks Ctrl+Esc, Alt+Tab/Alt+Shift+Tab, Alt+Esc/Alt+Shift+Esc, Ctrl+Shift+Esc, Alt+F4, and the Windows keys (left and right), sets the per-user registry policy that disables Task Manager, and strips the main window's title bar and system menu (the window becomes a borderless, maximized window).
- **On macOS:** hides the menu bar and Dock and disables Cmd+Tab app switching, Force Quit, and session termination, via `NSApplicationPresentationOptions`.

**Turning mode off** reverses all of the above: task tray/taskbar restored, keyboard hook removed, registry policy cleared, title bar restored (Windows); previous `NSApplicationPresentationOptions` restored (macOS, captured at the moment mode was last turned on).

### Example
From the plugin's own test method (`TEST.4dm`), which matches the usage shown in the plugin's README:
```4d
//%attributes = {}
If (0=KIOSK Get mode)
	KIOSK SET MODE(1)
Else 
	KIOSK SET MODE(0)
End if 
```

Turning kiosk mode on unconditionally, e.g. when a locked-down form loads:
```4d
KIOSK SET MODE(1)
```

Exiting kiosk mode with a confirmation, only if it's actually currently on:
```4d
If (1=KIOSK Get mode)
	ALERT("Exiting kiosk mode.")
	KIOSK SET MODE(0)
End if 
```

---

## KIOSK Get mode

### Syntax
```4d
KIOSK Get mode -> Longint
```

| Parameter | Type | Description |
|---|---|---|
| Result | Longint | `0` if kiosk mode is currently off, `1` if it's currently on. |

This command takes no parameters.

### Description
Reports the plugin's internally stored mode flag. It does not re-check the actual OS-level state (taskbar visibility, hook installation, registry policy) — it's purely a readback of the last value `KIOSK SET MODE` stored. If `KIOSK SET MODE` was previously called with a value other than `0` or `1`, this command returns that raw value unchanged rather than `0`/`1` — see [Error handling](#error-handling--troubleshooting).

### Example
```4d
If (KIOSK Get mode=1)
	ALERT("Kiosk mode is active.")
End if 
```

Toggling based on the current state (same pattern as `TEST.4dm` above, generalized):
```4d
$mode:=KIOSK Get mode
KIOSK SET MODE(1-$mode)  // flips 0<->1
```

---

## Error handling & troubleshooting

- **Silent failures, not 4D errors.** Neither command ever raises a 4D error. If the Windows keyboard hook fails to install, or the registry key needed to disable Task Manager can't be opened or created, `KIOSK SET MODE(1)` still completes normally, and `KIOSK Get mode` will still report `1` — 4D has no way to detect that the underlying OS-level lockdown was only partially applied.
- **Ctrl+Alt+Delete is never blocked.** It's handled by Windows' Secure Attention Sequence, outside the reach of the low-level keyboard hook this plugin installs. Task Manager access is instead denied via a registry policy, which is a separate mechanism from the hook.
- **Passing anything other than `0` or `1` to `KIOSK SET MODE`.** There's no validation — an out-of-range value is stored as-is (and echoed back by `KIOSK Get mode`), but no platform lockdown/restore action runs for it. Always pass exactly `0` or `1`.
- **No full-screen handling.** Kiosk mode changes window chrome and system shortcuts, but never resizes or repositions the window — do that yourself if you need true full-screen.
- **Secondary monitor taskbars stay visible on Windows.** Only the primary taskbar (`Shell_TrayWnd`) is hidden; per-monitor secondary taskbars on multi-monitor Windows 10/11 setups are unaffected.
- **The Windows registry policy can outlive an abnormal exit.** Kiosk mode sets the per-user policy `HKCU\Software\Microsoft\Windows\CurrentVersion\Policies\System\DisableTaskMgr`. It's cleared automatically by `KIOSK SET MODE(0)` or on a normal 4D quit. If 4D crashes or is force-terminated while still in kiosk mode, this value is **not** cleared — Task Manager stays disabled for that Windows user until the plugin runs its cleanup again (a normal exit, or an explicit `KIOSK SET MODE(0)`) or the value is removed manually.

---

## Quick reference

```4d
// Enter kiosk mode
KIOSK SET MODE(1)

// Exit kiosk mode
KIOSK SET MODE(0)

// Check current state
If (KIOSK Get mode=1)
	// currently in kiosk mode
End if 
```

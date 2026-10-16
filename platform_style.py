"""Small, optional OS material adapters; standard Qt controls stay native styled.

No private APIs, custom window borders, or replacement Linux theme are used.
"""
from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication


class WindowsBackdrop:
    """Request Mica on the native frame; Qt content surfaces remain opaque."""
    BACKDROP = 38
    DARK_MODE = 20

    def __init__(self):
        self.dwm = ctypes.WinDLL('dwmapi', use_last_error=True)
        self.set_attribute = self.dwm.DwmSetWindowAttribute
        self.set_attribute.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        self.set_attribute.restype = ctypes.c_long

    def set_value(self, hwnd, attribute, value):
        data = ctypes.c_int(value)
        return self.set_attribute(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data)) >= 0

    def apply(self, hwnd, dark):
        self.set_value(hwnd, self.DARK_MODE, int(dark))
        # Documented Windows 11 22H2+ API. DWM handles transparency preferences,
        # inactive windows, battery saver and high contrast itself.
        if sys.getwindowsversion().build < 22621:
            return 'Native Windows frame (Mica requires Windows 11 22H2)'
        if self.set_value(hwnd, self.BACKDROP, 2):  # DWMSBT_MAINWINDOW
            return 'Windows Mica frame'
        return 'Native Windows frame (Mica unavailable)'


class MacBackdrop:
    """Native AppKit material underneath Qt content, owned for the window lifetime."""
    def __init__(self, window):
        import AppKit
        import objc
        self.kit, self.objc = AppKit, objc
        self.window = window
        self.view = None
        self.native_window = None
        self.root = None
        self.original = None
        self.kind = 'Native macOS'
        self.original_translucent = window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.original_no_background = window.testAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        # Do this before the first NSView is created; changing Qt's surface
        # format after a window is visible is not portable.
        window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def reduced_effects(self):
        workspace = self.kit.NSWorkspace.sharedWorkspace()
        return (workspace.accessibilityDisplayShouldReduceTransparency()
                or workspace.accessibilityDisplayShouldIncreaseContrast())

    def apply(self, preference):
        if self.reduced_effects():
            self.clear()
            return 'Native macOS (accessibility: solid surfaces)'
        kit = self.kit
        if self.view is None:
            self.window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            root = self.objc.objc_object(c_void_p=int(self.window.winId()))
            native_window = root.window()
            parent = root.superview()
            if native_window is None or parent is None:
                self.clear()
                return 'Native macOS (window material unavailable)'
            glass_class = getattr(kit, 'NSGlassEffectView', None)
            if glass_class is not None:
                view = glass_class.alloc().initWithFrame_(root.frame())
                view.setCornerRadius_(12.0)
                self.kind = 'macOS Liquid Glass'
            else:
                view = kit.NSVisualEffectView.alloc().initWithFrame_(root.frame())
                view.setMaterial_(kit.NSVisualEffectMaterialUnderWindowBackground)
                view.setBlendingMode_(kit.NSVisualEffectBlendingModeBehindWindow)
                view.setState_(kit.NSVisualEffectStateFollowsWindowActiveState)
                self.kind = 'macOS vibrancy (Liquid Glass requires macOS 26)'
            self.root, self.native_window, self.view = root, native_window, view
            self.original = (native_window.isOpaque(), native_window.backgroundColor())
            # Retain Qt's NSView and normal traffic-light/title-bar ownership.
            # Place the material behind it, never reparent Qt's content view.
            parent.addSubview_positioned_relativeTo_(view, kit.NSWindowBelow, root)
            native_window.setOpaque_(False)
            native_window.setBackgroundColor_(kit.NSColor.clearColor())
            palette = self.window.palette()
            palette.setColor(QPalette.ColorRole.Window, Qt.GlobalColor.transparent)
            self.window.setPalette(palette)
        appearance = None
        if preference != 'System':
            name = self.kit.NSAppearanceNameDarkAqua if preference == 'Dark' else self.kit.NSAppearanceNameAqua
            appearance = self.kit.NSAppearance.appearanceNamed_(name)
        self.view.setAppearance_(appearance)
        self.resize()
        return self.kind

    def resize(self):
        if self.view is not None:
            self.view.setFrame_(self.root.frame())

    def clear(self):
        try:
            if self.view is not None:
                self.view.removeFromSuperview()
                self.native_window.setOpaque_(self.original[0])
                self.native_window.setBackgroundColor_(self.original[1])
        finally:
            self.view = self.root = self.native_window = None
            self.window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, self.original_translucent)
            self.window.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, self.original_no_background)
            # Remove our transparent Window brush while using the current OS palette.
            self.window.setPalette(QApplication.palette())
            self.window.update()


class PlatformStyle(QObject):
    """Apply native effects after HWND/NSView creation and on appearance changes."""
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.adapter = None
        self.preference = 'System'
        self.description = 'System desktop theme'
        self.closed = False
        self.pending = False
        self.failed = False
        if sys.platform == 'darwin' and QApplication.platformName() == 'cocoa':
            try:
                self.adapter = MacBackdrop(window)
            except Exception as exc:
                self.failed = True
                self.description = f'Native macOS; material unavailable: {exc}'
                window.logging(self.description)
        window.installEventFilter(self)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(2000)
        self.refresh_timer.timeout.connect(self.schedule)
        if sys.platform == 'darwin':
            # NSWorkspace accessibility preferences can change without a Qt
            # palette event. This cheap check also restores glass when reenabled.
            self.refresh_timer.start()
        QApplication.styleHints().colorSchemeChanged.connect(self.schedule)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Show, QEvent.Type.WinIdChange, QEvent.Type.PaletteChange):
            self.schedule()
        elif event.type() == QEvent.Type.Resize and self.adapter is not None and sys.platform == 'darwin':
            self.schedule()
        return False

    def schedule(self, *_):
        if not self.pending and not self.closed:
            self.pending = True
            QTimer.singleShot(0, self.apply)

    def set_appearance(self, preference):
        self.preference = preference
        self.schedule()

    def apply(self):
        self.pending = False
        if self.closed or self.failed:
            return
        # Never pass offscreen/minimal Qt handles to real OS APIs (including CI).
        platform = QApplication.platformName()
        if platform not in ('windows', 'cocoa'):
            self.description = f'System desktop theme ({QApplication.style().objectName()})'
            return
        try:
            for surface in getattr(self.window, 'material_surfaces', []):
                palette = surface.palette()
                palette.setColor(QPalette.ColorRole.Window, QApplication.palette().color(QPalette.ColorRole.Window))
                surface.setPalette(palette)
            if sys.platform == 'win32' and platform == 'windows':
                if self.adapter is None:
                    self.adapter = WindowsBackdrop()
                dark = self.preference == 'Dark' or (self.preference == 'System' and
                       QApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark)
                self.description = self.adapter.apply(int(self.window.winId()), dark)
            elif sys.platform == 'darwin' and platform == 'cocoa':
                if self.adapter is None:
                    self.adapter = MacBackdrop(self.window)
                self.description = self.adapter.apply(self.preference)
        except Exception as exc:
            # OS features are optional. A missing bridge or failed native call
            # must never prevent device automation or leave an invisible window.
            if isinstance(self.adapter, MacBackdrop):
                try:
                    self.adapter.clear()
                except Exception:
                    pass  # Qt's opaque fallback is restored in clear's finally.
            self.failed = True
            self.description = f'Native style; material unavailable: {exc}'
            if hasattr(self.window, 'logging'):
                self.window.logging(self.description)

    def close(self):
        self.closed = True
        self.refresh_timer.stop()
        if isinstance(self.adapter, MacBackdrop):
            self.adapter.clear()

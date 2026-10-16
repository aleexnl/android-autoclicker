"""Native API policy/lifecycle tests; actual macOS compositing needs a Mac."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import ctypes
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QApplication, QMainWindow
from PySide6.QtCore import Qt
from platform_style import WindowsBackdrop, MacBackdrop, PlatformStyle


class PlatformStyleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def test_offscreen_never_calls_native_apis(self):
        window = QMainWindow()
        with patch('platform_style.WindowsBackdrop') as windows, patch('platform_style.MacBackdrop') as mac:
            style = PlatformStyle(window)
            style.apply()
            windows.assert_not_called()
            mac.assert_not_called()
        style.close()
        window.close()

    def test_linux_preserves_selected_style(self):
        window = QMainWindow()
        original = self.qt.style().objectName()
        with patch('platform_style.sys.platform', 'linux'), patch.object(QApplication, 'platformName', return_value='wayland'):
            style = PlatformStyle(window)
            style.apply()
            self.assertEqual(original, self.qt.style().objectName())
            self.assertIn(original, style.description)
        style.close()
        window.close()

    def test_windows_version_gate_and_rejected_request(self):
        adapter = WindowsBackdrop.__new__(WindowsBackdrop)
        adapter.set_value = Mock(return_value=True)
        with patch('platform_style.sys.getwindowsversion', return_value=SimpleNamespace(build=19045), create=True):
            self.assertIn('requires', adapter.apply(42, True))
            adapter.set_value.assert_called_once_with(42, 20, 1)
        adapter.set_value.reset_mock()
        with patch('platform_style.sys.getwindowsversion', return_value=SimpleNamespace(build=22621), create=True):
            self.assertEqual(adapter.apply(42, False), 'Windows Mica frame')
            self.assertEqual(adapter.set_value.call_args.args, (42, 38, 2))
            adapter.set_value.return_value = False
            self.assertIn('unavailable', adapter.apply(42, False))

    def test_windows_hresult_is_signed_and_passes_int_payload(self):
        adapter = WindowsBackdrop.__new__(WindowsBackdrop)
        values = []
        def setter(hwnd, attribute, data, size):
            values.append((hwnd, attribute, ctypes.cast(data, ctypes.POINTER(ctypes.c_int)).contents.value, size))
            return -1
        adapter.set_attribute = setter
        self.assertFalse(adapter.set_value(123, 38, 2))
        self.assertEqual(values, [(123, 38, 2, 4)])

    def make_mac(self, glass=True):
        window = QMainWindow()
        kit, objc = Mock(), Mock()
        workspace = kit.NSWorkspace.sharedWorkspace.return_value
        workspace.accessibilityDisplayShouldReduceTransparency.return_value = False
        workspace.accessibilityDisplayShouldIncreaseContrast.return_value = False
        root = objc.objc_object.return_value
        root.window.return_value.isOpaque.return_value = True
        if not glass:
            kit.NSGlassEffectView = None
        with patch.dict('sys.modules', {'AppKit': kit, 'objc': objc}):
            adapter = MacBackdrop(window)
        return window, adapter, kit, root, workspace

    def test_mac_glass_is_not_duplicated_and_cleanup_restores_opaque_window(self):
        window, adapter, kit, root, workspace = self.make_mac()
        try:
            self.assertEqual(adapter.apply('Dark'), 'macOS Liquid Glass')
            view = adapter.view
            adapter.apply('System')
            root.superview.return_value.addSubview_positioned_relativeTo_.assert_called_once()
            view.setAppearance_.assert_called_with(None)
            adapter.resize()
            view.setFrame_.assert_called_with(root.frame())
            adapter.clear()
            view.removeFromSuperview.assert_called_once()
            root.window.return_value.setOpaque_.assert_called_with(True)
            self.assertFalse(window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        finally:
            adapter.clear()
            window.close()

    def test_mac_reduced_transparency_removes_effect_and_can_restore(self):
        window, adapter, kit, root, workspace = self.make_mac()
        try:
            adapter.apply('System')
            workspace.accessibilityDisplayShouldReduceTransparency.return_value = True
            self.assertIn('solid', adapter.apply('System'))
            self.assertIsNone(adapter.view)
            self.assertFalse(window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
            workspace.accessibilityDisplayShouldReduceTransparency.return_value = False
            self.assertEqual(adapter.apply('Light'), 'macOS Liquid Glass')
        finally:
            adapter.clear()
            window.close()

    def test_older_mac_uses_vibrancy(self):
        window, adapter, kit, root, workspace = self.make_mac(glass=False)
        try:
            self.assertIn('vibrancy', adapter.apply('System'))
            kit.NSVisualEffectView.alloc.assert_called_once()
        finally:
            adapter.clear()
            window.close()

    def test_mac_missing_native_parent_restores_solid_background(self):
        window, adapter, kit, root, workspace = self.make_mac()
        root.superview.return_value = None
        try:
            self.assertIn('unavailable', adapter.apply('System'))
            self.assertFalse(window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        finally:
            window.close()


if __name__ == '__main__':
    unittest.main()

"""Regression tests for workflow compatibility and background device operations."""
import copy
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import numpy as np
from PIL import Image
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox
import app as editor
import engine
from workflow_model import new_step


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        rng = np.random.default_rng(42)
        self.template = Image.fromarray(rng.integers(0, 256, (20, 30, 3), dtype=np.uint8))
        self.template.save(self.folder / 'target.png')
        self.screen = Image.new('RGB', (120, 100))
        self.screen.paste(self.template, (40, 30))

    def tearDown(self):
        self.tmp.cleanup()

    def test_match_prepared_template_and_region_coordinates(self):
        for template in (self.template, engine.prepare_template(self.template)):
            score, x, y = engine.match_image(self.screen, template, 0.9, [35, 25, 60, 50])
            self.assertGreater(score, 0.99)
            self.assertEqual((x, y), (55, 40))

    def test_template_cached_across_loop_and_reloaded_next_run(self):
        step = dict(new_step('find_image', set()), image='target.png', next='step_1')
        adb = Mock()
        adb.screenshot.return_value = self.screen
        for _ in range(2):
            stop = threading.Event()
            count = 0
            def tapped(*args):
                nonlocal count
                count += 1
                if count == 2:
                    stop.set()
            adb.tap.side_effect = tapped
            with patch('engine.prepare_template', wraps=engine.prepare_template) as prepare:
                with self.assertRaises(InterruptedError):
                    engine.run_workflow([step], adb, self.folder, stop, lambda _: None)
                self.assertEqual(prepare.call_count, 1)

    def test_stop_during_capture_does_not_tap(self):
        stop = threading.Event()
        adb = Mock()
        def capture():
            stop.set()
            return self.screen
        adb.screenshot.side_effect = capture
        step = dict(new_step('find_image', set()), image='target.png')
        with self.assertRaises(InterruptedError):
            engine.run_workflow([step], adb, self.folder, stop, lambda _: None)
        adb.tap.assert_not_called()

    def test_branching_and_all_input_actions(self):
        steps = [dict(new_step('find_image', set()), image='target.png', on_true='tap', on_false='end', click_match=False),
                 {'id': 'tap', 'type': 'tap', 'x': 3, 'y': 4},
                 {'id': 'swipe', 'type': 'swipe', 'x': 1, 'y': 2, 'x2': 3, 'y2': 4},
                 {'id': 'key', 'type': 'key', 'key': 'KEYCODE_BACK'},
                 {'id': 'text', 'type': 'text', 'text': 'hello'},
                 {'id': 'wait', 'type': 'wait', 'seconds': 0},
                 {'id': 'jump', 'type': 'goto', 'next': 'end'},
                 {'id': 'skipped', 'type': 'tap'}, {'id': 'end', 'type': 'stop'}]
        adb = Mock()
        adb.screenshot.return_value = self.screen
        engine.run_workflow(steps, adb, self.folder, threading.Event(), lambda _: None)
        adb.tap.assert_called_once_with(3, 4)
        adb.swipe.assert_called_once_with(1, 2, 3, 4, 400)
        adb.key.assert_called_once_with('KEYCODE_BACK')
        adb.text.assert_called_once_with('hello')

    def test_workflow_json_round_trip(self):
        steps = [dict(new_step('find_image', set()), image='target.png', region=[1, 2, 30, 40])]
        target = self.folder / 'workflow.json'
        engine.save_workflow(target, steps)
        self.assertEqual(engine.load_workflow(target), steps)

    def test_platform_adb_filename_and_override(self):
        for platform, filename in [('win32', 'adb.exe'), ('linux', 'adb'), ('darwin', 'adb')]:
            with patch('engine.sys.platform', platform), patch.object(Path, 'is_file', return_value=True):
                self.assertEqual(Path(engine.ADB().executable).name, filename)
                self.assertEqual(engine.ADB(executable='/custom/adb').executable, '/custom/adb')


class EditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = editor.Editor(startup=False)
        self.window.show()
        self.qt.processEvents()

    def tearDown(self):
        self.window.stop_event.set()
        self.pump(lambda: not any(t.is_alive() for t in self.window.workers))
        self.window.process_messages()
        self.window.close()

    def pump(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.qt.processEvents()
            QTest.qWait(5)
        self.assertTrue(condition(), 'Timed out waiting for background task')

    def test_region_round_trip_and_rename_branches(self):
        step = self.window.steps[0]
        step['region'] = [1, 2, 30, 40]
        self.window.steps[1]['next'] = step['id']
        self.window.on_select()
        self.assertEqual(self.window.vars['region'].text(), '1,2,30,40')
        self.window.vars['id'].setText('renamed')
        self.assertTrue(self.window.apply())
        self.assertEqual(step['region'], [1, 2, 30, 40])
        self.assertEqual(self.window.steps[1]['next'], 'renamed')

    def test_action_change_populates_defaults(self):
        self.window.vars['type'].setCurrentText('swipe')
        self.assertEqual(self.window.vars['duration'].text(), '450')
        self.assertTrue(self.window.apply())
        self.assertEqual(self.window.steps[0]['type'], 'swipe')

    def test_save_as_preserves_template_and_failed_save_keeps_path(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            self.window.workflow_path = base / 'old.json'
            self.window.steps[0]['image'] = 'target.png'
            self.window.on_select()
            target = base / 'new' / 'workflow.json'
            self.assertTrue(self.window.write_workflow(target))
            self.assertEqual(self.window.steps[0]['image'], str(base / 'target.png'))
            self.assertEqual(engine.load_workflow(target), self.window.steps)
            with patch('app.save_workflow', side_effect=OSError('read only')), patch.object(QMessageBox, 'warning'):
                self.assertFalse(self.window.write_workflow(base / 'failed.json'))
            self.assertEqual(self.window.workflow_path, target)

    def test_slow_device_operation_does_not_block_event_loop(self):
        release = threading.Event()
        result = []
        main_thread = threading.get_ident()
        worker_threads = []
        def work():
            worker_threads.append(threading.get_ident())
            release.wait(1)
            return ['device-1']
        ticks = []
        QTimer.singleShot(10, lambda: ticks.append(True))
        self.window.background('Testing', work, lambda value: result.append((threading.get_ident(), value)))
        try:
            self.pump(lambda: bool(ticks))
            self.assertFalse(result)
            self.assertFalse(self.window.start_btn.isEnabled())
        finally:
            release.set()
        self.pump(lambda: bool(result))
        self.assertNotEqual(worker_threads[0], main_thread)
        self.assertEqual(result, [(main_thread, ['device-1'])])
        self.assertTrue(self.window.start_btn.isEnabled())

    def test_device_failure_recovers_controls(self):
        with patch.object(QMessageBox, 'warning') as warning:
            self.window.background('Failing', lambda: (_ for _ in ()).throw(OSError('ADB missing')), lambda _: None)
            self.pump(lambda: not self.window.busy)
            warning.assert_called_once()
        self.assertTrue(self.window.start_btn.isEnabled())

    def test_run_and_stop_and_close_wait_for_worker(self):
        self.window.steps = [dict(new_step('wait', set()), seconds=30)]
        self.window.refresh_tree('step_1')
        self.window.device.setCurrentText('fake-device')
        self.window.run()
        self.assertTrue(self.window.running)
        self.window.stop()
        self.pump(lambda: not self.window.running)
        self.assertEqual(self.window.statusBar().currentMessage(), 'Stopped by user')
        release = threading.Event()
        self.window.background('Closing', lambda: release.wait(1), lambda _: self.fail('Callback after close'))
        self.window.close()
        self.assertTrue(self.window.closing)
        release.set()
        self.pump(lambda: not self.window.isVisible())

    def test_switch_step_applies_pending_edits_and_renamed_branches(self):
        self.window.steps[1]['next'] = 'step_1'
        self.window.vars['id'].setText('renamed')
        self.window.vars['threshold'].setText('0.91')
        self.window.tree.setCurrentItem(self.window.tree.topLevelItem(1))
        self.assertEqual(self.window.steps[0]['id'], 'renamed')
        self.assertEqual(self.window.steps[0]['threshold'], 0.91)
        self.assertEqual(self.window.vars['next'].text(), 'renamed')
        self.assertEqual(self.window.get_selected()['id'], 'step_2')
        self.assertFalse(self.window.form_dirty)

    def test_invalid_draft_blocks_selection_and_revert_restores_values(self):
        self.window.vars['threshold'].setText('invalid')
        with patch.object(QMessageBox, 'warning') as warning:
            self.window.tree.setCurrentItem(self.window.tree.topLevelItem(1))
        warning.assert_called_once()
        self.assertEqual(self.window.get_selected()['id'], 'step_1')
        self.assertEqual(self.window.vars['threshold'].text(), 'invalid')
        self.assertTrue(self.window.form_dirty)
        self.window.revert_form()
        self.assertEqual(self.window.vars['threshold'].text(), '0.88')
        self.assertFalse(self.window.apply_button.isEnabled())

    def test_clone_includes_pending_edits(self):
        self.window.vars['threshold'].setText('0.93')
        self.window.clone_step()
        self.assertEqual(self.window.steps[0]['threshold'], 0.93)
        self.assertEqual(self.window.get_selected()['threshold'], 0.93)
        self.assertNotEqual(self.window.get_selected()['id'], 'step_1')

    def test_empty_workflow_disables_editor_and_add_restores_it(self):
        self.window.steps.clear()
        self.window.refresh_tree()
        self.assertFalse(self.window.vars['id'].isEnabled())
        self.assertTrue(all(not b.isEnabled() for b in self.window.step_buttons))
        self.window.add_step()
        self.assertTrue(self.window.vars['id'].isEnabled())
        self.assertEqual(len(self.window.steps), 1)

    def test_failure_reveals_collapsed_log(self):
        self.window.log_toggle.setChecked(False)
        self.window.messages.put(('done', 'Failed — see execution log'))
        self.window.process_messages()
        self.assertTrue(self.window.log_view.isVisible())
        self.assertEqual(self.window.run_state.text(), 'Failed — see execution log')

    def test_crop_scales_back_to_device_pixels(self):
        canvas = editor.CropCanvas(Image.new('RGB', (1800, 1300)))
        selected = []
        canvas.selected.connect(selected.append)
        canvas.show()
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(100, 80))
        QTest.mouseMove(canvas, QPoint(250, 200))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(250, 200))
        self.assertEqual(selected, [(200, 160, 500, 400)])
        canvas.close()

    def test_hotkey_import_failure_does_not_prevent_editor(self):
        with patch.dict('sys.modules', {'pynput': None}):
            self.window.start_hotkey()
        self.assertIn('unavailable', self.window.hotkey_status.text())


if __name__ == '__main__':
    unittest.main()

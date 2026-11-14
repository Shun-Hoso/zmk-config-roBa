import importlib.util
import contextlib
import io
import os
import pty
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collector = load('collect')
prepare = load('prepare_sources')
verify = load('verify_build')


class CollectorPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.record = dict.fromkeys(collector.FIELDS, 0)
        self.record.update(seq=1, ms=1000, span=1000, usb=1, irq_pin=-5)
        self.line = collector.format_record(self.record).encode('ascii')

    def test_accepts_only_complete_numeric_schema(self):
        self.assertEqual(collector.parse_record(self.line + b'\r\n'), self.record)

    def test_rejects_previous_firmware_schema(self):
        self.assertIsNone(collector.parse_record(self.line.replace(b'ROBA_DIAG_V2', b'ROBA_DIAG_V1')))

    def test_rejects_keyboard_logs_and_unknown_fields(self):
        for data in (b'keycode=42 text=secret', self.line + b' keycode=42',
                     self.line.replace(b' ms=1000', b' text=1000')):
            self.assertIsNone(collector.parse_record(data))

    def test_rejects_duplicate_fields_and_partial_lines(self):
        for data in (self.line.replace(b' ms=1000', b' seq=1000'), self.line[:-4]):
            self.assertIsNone(collector.parse_record(data))

    def test_rejects_control_sequences_and_unbounded_numbers(self):
        for data in (self.line + b'\x1b[0m', self.line.replace(b'seq=1', b'seq=99999999999'),
                     self.line.replace(b'seq=1', b'seq=-1'), b'x' * 2000):
            self.assertIsNone(collector.parse_record(data))

    def test_selects_only_serial_port_under_right_roba(self):
        def device(name, vendor, port):
            return {'IOObjectClass': 'IOUSBHostDevice', 'USB Product Name': name,
                    'idVendor': vendor, 'idProduct': 0x615E,
                    'IORegistryEntryChildren': [{'IOObjectClass': 'IOUSBHostInterface',
                        'idVendor': vendor, 'idProduct': 0x615E, 'IORegistryEntryChildren': [
                        {'IOCalloutDevice': port}]}]}
        tree = [device('roBa_L', 0x1D50, '/dev/cu.usbmodemLEFT'),
                device('roBa', 0x1D50, '/dev/cu.usbmodemRIGHT'),
                device('roBa', 0x1234, '/dev/cu.usbmodemOTHER'),
                device('Other', 0x1D50, '/dev/cu.usbmodemMODEM')]
        self.assertEqual(collector.roba_ports(tree), ['/dev/cu.usbmodemRIGHT'])

    def test_serial_capture_discards_unrelated_text(self):
        master, slave = pty.openpty()

        def write_records():
            time.sleep(0.2)
            for _ in range(3):
                os.write(master, b'keycode=42 private-text=DO_NOT_SAVE\n' + self.line + b'\r\n')
                time.sleep(0.1)

        writer = threading.Thread(target=write_records)
        try:
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / 'capture.txt'
                writer.start()
                with patch.object(collector.fcntl, 'ioctl', return_value=0), \
                        contextlib.redirect_stdout(io.StringIO()):
                    count = collector.collect(os.ttyname(slave), output, 1)
                writer.join()
                saved = output.read_text()
                self.assertEqual(count, 3)
                self.assertEqual(saved.count(collector.PREFIX), 3)
                self.assertNotIn('DO_NOT_SAVE', saved)
                self.assertNotIn('keycode=42', saved)
        finally:
            writer.join(timeout=2)
            os.close(master)
            os.close(slave)


class InstrumentationTests(unittest.TestCase):
    def test_rejects_unknown_upstream_without_partial_write(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            driver = workspace / 'zmk-pmw3610-driver/src/pmw3610.c'
            listener = workspace / 'zmk/app/src/pointing/input_listener.c'
            for file in (driver, listener):
                file.parent.mkdir(parents=True)
                file.write_text('unsupported revision\n')
            with self.assertRaisesRegex(ValueError, 'Unrecognized source revision'):
                prepare.prepare(workspace)
            self.assertEqual(driver.read_text(), 'unsupported revision\n')
            self.assertEqual(listener.read_text(), 'unsupported revision\n')

    def test_rejects_missing_or_duplicate_hook_sites(self):
        with self.assertRaises(ValueError):
            prepare.replace_exact('abc abc', 'abc', 'xyz')
        with self.assertRaises(ValueError):
            prepare.replace_exact('abc', 'missing', 'xyz')


class BuildGuardTests(unittest.TestCase):
    def setUp(self):
        self.normal = {'CONFIG_PMW3610_CPI': '400'}
        self.diagnostic = dict(self.normal, CONFIG_ROBA_DIAGNOSTICS='y',
                               CONFIG_USB_CDC_ACM='y', CONFIG_ZMK_USB='y')

    def test_accepts_private_diagnostic(self):
        verify.verify(self.normal, self.diagnostic)

    def test_rejects_logging_or_studio(self):
        for key in ('CONFIG_LOG', 'CONFIG_PRINTK', 'CONFIG_CONSOLE', 'CONFIG_BOOT_BANNER',
                    'CONFIG_ZMK_STUDIO'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                verify.verify(self.normal, dict(self.diagnostic, **{key: 'y'}))

    def test_rejects_sensitivity_or_scheduling_changes(self):
        for key, value in [('CONFIG_PMW3610_CPI', '800'), ('CONFIG_INPUT_QUEUE_MAX_MSGS', '64')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                verify.verify(self.normal, dict(self.diagnostic, **{key: value}))


if __name__ == '__main__':
    unittest.main()

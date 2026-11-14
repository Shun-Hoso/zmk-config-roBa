import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FirmwareFormatTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('cc'), 'Native C compiler is unavailable')
    def test_firmware_format_matches_collector_even_at_counter_limit(self):
        source = (ROOT / 'src/roba_diagnostics.c').read_text()
        fields = source[source.index('#define DIAG_FIELDS'):source.index('struct pending_operation')]
        start = source.index('        char line[1024];')
        formatting = source[start:source.index('        if (length < 0', start)]
        formatting = formatting.replace(
            'zmk_endpoints_selected().transport == ZMK_TRANSPORT_USB', '1')
        formatting = formatting.replace('gpio_pin_get_dt(&motion_pin)', '0')
        program = '''#include <stdio.h>
#include <stdint.h>
#include <string.h>
#undef snprintf
#define snprintf(...) __builtin_snprintf(__VA_ARGS__)
''' + fields + '''int main(void) {
struct diagnostic_counts snapshot;
memset(&snapshot, 255, sizeof(snapshot));
unsigned sequence=UINT32_MAX, now=UINT32_MAX, span=UINT32_MAX, enabled=1, scrolling=0;
unsigned queue_age=UINT32_MAX, work_age=UINT32_MAX, read_age=UINT32_MAX;
unsigned post_age=UINT32_MAX, process_age=UINT32_MAX, send_age=UINT32_MAX;
unsigned output_dropped=UINT32_MAX;
''' + formatting + '''if (length <= 0 || length >= (int)sizeof(line)) return 1;
fputs(line, stdout);
return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            c_file = Path(directory) / 'format.c'
            executable = Path(directory) / 'format'
            c_file.write_text(program)
            subprocess.run(['cc', '-std=c99', '-Wall', '-Wextra', '-Werror',
                            str(c_file), '-o', str(executable)], check=True, capture_output=True)
            line = subprocess.check_output([str(executable)])

        spec = importlib.util.spec_from_file_location('collect', ROOT / 'collect.py')
        collector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(collector)
        self.assertLess(len(line), 1024)
        self.assertIsNotNone(collector.parse_record(line))

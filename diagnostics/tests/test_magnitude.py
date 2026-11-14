from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MovementAccountingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('cc'), 'Native C compiler is unavailable')
    def test_tracks_unsigned_totals_without_counting_scroll_or_failed_posts(self):
        source = (ROOT / 'src/roba_diagnostics.c').read_text()
        state = source[source.index('#define DIAG_FIELDS'):source.index('static const struct device')]
        hooks = source[source.index('static void begin('):source.index('/* USBが')]
        program = '''#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#define MAX(a, b) ((a) > (b) ? (a) : (b))
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define INPUT_REL_X 0
#define INPUT_REL_Y 1
#define INPUT_REL_WHEEL 8
struct device { int unused; };
struct k_spinlock { int unused; };
typedef int k_spinlock_key_t;
typedef int k_timeout_t;
static struct device ball, other;
static const struct device *const trackball = &ball;
static int post_result;
static k_spinlock_key_t k_spin_lock(struct k_spinlock *lock) { (void)lock; return 0; }
static void k_spin_unlock(struct k_spinlock *lock, k_spinlock_key_t key) {
    (void)lock; (void)key;
}
static uint32_t k_uptime_get_32(void) { return 100; }
static int input_report_rel(const struct device *dev, uint16_t code, int32_t value,
                            bool sync, k_timeout_t timeout) {
    (void)dev; (void)code; (void)value; (void)sync; (void)timeout;
    return post_result;
}
''' + state + hooks + '''
int main(void) {
    /* 往復の動きを符号付きで足すと0になるが、移動量は14。 */
    roba_diag_motion(0, 3, -4, 100, false, true);
    roba_diag_motion(0, -3, 4, 100, false, true);
    roba_diag_report_rel(trackball, INPUT_REL_X, 3, false, 0);
    roba_diag_report_rel(trackball, INPUT_REL_Y, -4, true, 0);
    roba_diag_report_rel(trackball, INPUT_REL_X, -3, false, 0);
    roba_diag_report_rel(trackball, INPUT_REL_Y, 4, true, 0);
    roba_diag_send_begin(trackball, 3, -4, true);
    roba_diag_send_begin(trackball, -3, 4, true);
    assert(counts.sensor_abs == 14 && counts.post_abs == 14 && counts.report_abs == 14);

    /* エラー、スクロール、別デバイスはポインタ移動量へ混ぜない。 */
    roba_diag_motion(-5, 10, 20, 0, false, true);
    roba_diag_motion(0, 10, 20, 100, true, false);
    roba_diag_report_rel(trackball, INPUT_REL_WHEEL, 50, true, 0);
    post_result = -5;
    assert(roba_diag_report_rel(trackball, INPUT_REL_X, 50, true, 0) == -5);
    roba_diag_send_begin(trackball, 50, 50, false);
    roba_diag_send_begin(&other, 50, 50, true);
    assert(counts.sensor_abs == 14 && counts.post_abs == 14 && counts.report_abs == 14);
    assert(counts.other_mode_reads == 1 && counts.read_err == 1 && counts.post_err == 1);

    /* 途中で移動量が0になるケースも、検出回数と区別できる。 */
    roba_diag_motion(0, 5, 0, 100, false, true);
    post_result = 0;
    roba_diag_report_rel(trackball, INPUT_REL_X, 5, true, 0);
    roba_diag_send_begin(trackball, 0, 0, true);
    assert(counts.sensor_abs == 19 && counts.post_abs == 19 && counts.report_abs == 14);
    assert(counts.report_zero == 1);
    assert(magnitude(INT32_MIN) == 2147483648u);
    roba_diag_motion(0, INT16_MIN, INT16_MIN, 100, false, true);
    assert(counts.sensor_abs == 65555);

    counts.sensor_abs = UINT32_MAX - 2;
    roba_diag_motion(0, 5, 0, 100, false, true);
    assert(counts.sensor_abs == 2);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            c_file = Path(directory) / 'accounting.c'
            executable = Path(directory) / 'accounting'
            c_file.write_text(program)
            subprocess.run(['cc', '-std=c99', '-Wall', '-Wextra', '-Werror',
                            '-fsanitize=undefined', str(c_file), '-o', str(executable)],
                           check=True, capture_output=True)
            subprocess.run([str(executable)], check=True, capture_output=True)

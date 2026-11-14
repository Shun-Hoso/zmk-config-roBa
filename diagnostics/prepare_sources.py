#!/usr/bin/env python3
"""隔離したwest作業ディレクトリにだけ計測箇所を追加する。"""

import argparse
import hashlib
from pathlib import Path


def replace_exact(source, before, after, count=1):
    actual = source.count(before)
    if actual != count:
        raise ValueError(f"Expected {count} instrumentation sites, found {actual}: {before[:80]!r}")
    return source.replace(before, after)


def instrument_driver(source):
    source = replace_exact(source, '#include "pmw3610.h"',
                           '#include "pmw3610.h"\n#include <roba_diagnostics.h>')
    source = replace_exact(source,
        'en ? GPIO_INT_LEVEL_ACTIVE : GPIO_INT_DISABLE);',
        'en ? GPIO_INT_LEVEL_ACTIVE : GPIO_INT_DISABLE);\n'
        '    roba_diag_irq_configured(en, ret);')
    source = replace_exact(source,
        '    int err = motion_burst_read(dev, buf, sizeof(buf));\n    if (err) {\n        return err;\n    }',
        '    roba_diag_read_begin();\n'
        '    int err = motion_burst_read(dev, buf, sizeof(buf));\n'
        '    if (err) {\n'
        '        roba_diag_motion(err, 0, 0, 0, input_mode == SCROLL, false);\n'
        '        return err;\n    }')
    source = replace_exact(source,
        '    int16_t raw_x =',
        '    roba_diag_motion(0,\n'
        '        TOINT16((buf[PMW3610_X_L_POS] + ((buf[PMW3610_XY_H_POS] & 0xF0) << 4)), 12),\n'
        '        TOINT16((buf[PMW3610_Y_L_POS] + ((buf[PMW3610_XY_H_POS] & 0x0F) << 8)), 12),\n'
        '        buf[4], input_mode == SCROLL, input_mode == MOVE || input_mode == SNIPE);\n\n'
        '    int16_t raw_x =')
    source = replace_exact(source, 'input_report_rel(', 'roba_diag_report_rel(', count=4)
    source = replace_exact(source,
        '    set_interrupt(dev, false);\n\n    // submit the real handler work\n'
        '    k_work_submit(&data->trigger_work);',
        '    roba_diag_irq();\n'
        '    set_interrupt(dev, false);\n\n    // submit the real handler work\n'
        '    int result = k_work_submit(&data->trigger_work);\n'
        '    roba_diag_submitted(result);')
    source = replace_exact(source,
        '    pmw3610_report_data(dev);\n    set_interrupt(dev, true);',
        '    roba_diag_work_begin();\n'
        '    pmw3610_report_data(dev);\n    set_interrupt(dev, true);\n'
        '    roba_diag_work_end();')
    return source


def instrument_listener(source):
    source = replace_exact(source, '#include <zmk/endpoints.h>',
                           '#include <zmk/endpoints.h>\n#include <roba_diagnostics.h>')
    source = replace_exact(source,
        '    int ret = filter_with_input_config(config, data, evt);',
        '    roba_diag_process_begin(evt->dev, evt->sync);\n'
        '    int ret = filter_with_input_config(config, data, evt);\n'
        '    roba_diag_process_end(evt->dev, ret != ZMK_INPUT_PROC_CONTINUE);')
    source = replace_exact(source, '        zmk_endpoints_send_mouse_report();',
        '        roba_diag_send_begin(evt->dev,\n'
        '            zmk_hid_get_mouse_report()->body.d_x,\n'
        '            zmk_hid_get_mouse_report()->body.d_y,\n'
        '            data->mouse.data.mode == INPUT_LISTENER_XY_DATA_MODE_REL);\n'
        '        int send_result = zmk_endpoints_send_mouse_report();\n'
        '        roba_diag_send_end(evt->dev, send_result);')
    return source


def prepare(workspace):
    files = [
        (workspace / 'zmk-pmw3610-driver/src/pmw3610.c', instrument_driver,
         'e5b86ebdf865082d7254195a94158e5667d8c6da'),
        (workspace / 'zmk/app/src/pointing/input_listener.c', instrument_listener,
         '28f2530003257074ee32e72603a470ca4cfaed05'),
    ]
    # 片方だけが書き換わらないよう、全箇所を確認してから保存する。
    prepared = []
    for path, instrument, expected in files:
        original = path.read_bytes()
        blob = b'blob ' + str(len(original)).encode('ascii') + b'\0' + original
        if hashlib.sha1(blob).hexdigest() != expected:
            raise ValueError(f"Unrecognized source revision: {path.name}")
        prepared.append((path, instrument(original.decode('utf-8'))))
    for path, source in prepared:
        path.write_text(source)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workspace', type=Path)
    prepare(parser.parse_args().workspace)

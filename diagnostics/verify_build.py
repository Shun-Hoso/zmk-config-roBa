#!/usr/bin/env python3
"""診断版の情報制限と、通常版との操作設定の一致を確認する。"""

import sys
from pathlib import Path


def config(path):
    return dict(line.split('=', 1) for line in Path(path).read_text().splitlines()
                if line.startswith('CONFIG_') and '=' in line)


def verify(normal, diagnostic):
    for key in ('CONFIG_LOG', 'CONFIG_PRINTK', 'CONFIG_CONSOLE', 'CONFIG_ZMK_STUDIO',
                'CONFIG_ZMK_USB_LOGGING', 'CONFIG_ZMK_RTT_LOGGING'):
        if diagnostic.get(key, 'n') != 'n':
            raise ValueError(f"Privacy check failed: {key}")
    for key in ('CONFIG_ROBA_DIAGNOSTICS', 'CONFIG_USB_CDC_ACM', 'CONFIG_ZMK_USB'):
        if diagnostic.get(key) != 'y':
            raise ValueError(f"Diagnostic interface missing: {key}")
    if normal.get('CONFIG_ROBA_DIAGNOSTICS', 'n') != 'n':
        raise ValueError('Restore build must not contain diagnostics')
    for key in set(normal) | set(diagnostic):
        if key.startswith('CONFIG_PMW3610_') and 'LOG_LEVEL' not in key:
            if normal.get(key, 'n') != diagnostic.get(key, 'n'):
                raise ValueError(f"Sensor behavior changed: {key}")
    for key in ('CONFIG_INPUT_MODE_THREAD', 'CONFIG_INPUT_THREAD_PRIORITY',
                'CONFIG_INPUT_QUEUE_MAX_MSGS', 'CONFIG_SYSTEM_WORKQUEUE_PRIORITY'):
        if normal.get(key) != diagnostic.get(key):
            raise ValueError(f"Input scheduling changed: {key}")


if __name__ == '__main__':
    verify(config(sys.argv[1]), config(sys.argv[2]))
    print('Diagnostic privacy and sensor configuration checks passed.')

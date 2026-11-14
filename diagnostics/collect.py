#!/usr/bin/env python3
"""macOSで右側roBaの診断用USB出力だけを90秒間保存する。外部ライブラリ不要。"""

import argparse
from datetime import datetime
import fcntl
import os
from pathlib import Path
import plistlib
import re
import select
import struct
import subprocess
import sys
import termios
import time
import tty

PREFIX = 'ROBA_DIAG_V1'
FIELDS = (
    'seq ms span usb irq_on irq_pin scroll queue_age_ms work_age_ms read_age_ms '
    'post_age_ms process_age_ms send_age_ms output_drop irq work done irq_err submit_err '
    'reads moving zero read_err quality_min quality_max queue_max_ms read_max_ms post '
    'post_err post_max_ms events frames filtered process_max_ms send send_err send_max_ms'
).split()


def parse_record(line):
    """未知の項目・文字列・制御文字を含む行は保存しない。"""
    if len(line) > 1024:
        return None
    try:
        text = line.decode('ascii').rstrip('\r\n')
    except UnicodeDecodeError:
        return None
    parts = text.split(' ')
    if len(parts) != len(FIELDS) + 1 or parts[0] != PREFIX:
        return None
    record = {}
    for part in parts[1:]:
        match = re.fullmatch(r'([a-z_]+)=(-?[0-9]{1,10})', part)
        if not match:
            return None
        key, number = match.groups()
        if key not in FIELDS or key in record:
            return None
        value = int(number)
        if not (-2147483648 if key == 'irq_pin' else 0) <= value <= 4294967295:
            return None
        record[key] = value
    return record if set(record) == set(FIELDS) else None


def format_record(record):
    return PREFIX + ' ' + ' '.join(f'{key}={record[key]}' for key in FIELDS)


def roba_ports(tree):
    """roBaのUSB機器の子にあるシリアル端子だけを候補にする。"""
    ports = set()

    def visit(node, under_roba=False):
        if not isinstance(node, dict):
            return
        product = node.get('USB Product Name')
        if node.get('IOObjectClass') == 'IOUSBHostDevice':
            under_roba = (product == 'roBa' and node.get('idVendor') == 0x1D50
                          and node.get('idProduct') == 0x615E)
        port = node.get('IOCalloutDevice', '')
        if under_roba and re.fullmatch(r'/dev/cu\.usbmodem[A-Za-z0-9_-]+', port):
            ports.add(port)
        for child in node.get('IORegistryEntryChildren', []):
            visit(child, under_roba)

    for node in tree:
        visit(node)
    return sorted(ports)


def find_port():
    result = subprocess.run(['ioreg', '-a', '-l', '-r', '-c', 'IOUSBHostDevice'],
                            capture_output=True, check=True)
    ports = [port for port in roba_ports(plistlib.loads(result.stdout)) if os.path.exists(port)]
    if len(ports) != 1:
        raise RuntimeError('右側roBaのUSB端子を1つに特定できません。右側をUSBで接続し、'
                           '診断版を書き込んだことを確認してください。')
    return ports[0]


def phase_at(elapsed):
    if elapsed < 5:
        return 'rest1', '球に触れず、5秒ほど待ってください。'
    if elapsed < 30:
        return 'move1', '右の球を同じ方向へ繰り返し転がしてください。ポインタが止まっても続けます。'
    if elapsed < 40:
        return 'rest2', '球から手を離してください。'
    if elapsed < 65:
        return 'move2', 'もう一度、右の球を繰り返し転がしてください。'
    return 'normal', '残りは、普段困る細かい操作や大きな移動を試してください。'


def collect(port, output, seconds):
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    previous = None
    count = 0
    try:
        previous = termios.tcgetattr(fd)
        tty.setraw(fd, termios.TCSANOW)
        attributes = termios.tcgetattr(fd)
        attributes[4] = attributes[5] = termios.B115200
        termios.tcsetattr(fd, termios.TCSANOW, attributes)
        termios.tcflush(fd, termios.TCIFLUSH)
        fcntl.ioctl(fd, termios.TIOCMBIS, struct.pack('I', termios.TIOCM_DTR))

        connected_at = time.monotonic()
        started_at = None
        pending = b''
        previous_phase = None
        with output.open('x', encoding='utf-8') as log:
            log.write('# roBa diagnostic counters v1; no keycodes or coordinates\n')
            print('診断データを待っています。通常版の出力は保存しません。', flush=True)
            while True:
                now = time.monotonic()
                if started_at is None:
                    if now - connected_at > 15:
                        raise RuntimeError('診断データを受信できませんでした。診断版の書き込みと、'
                                           'Studioなどで同じUSB端子を開いていないか確認してください。')
                else:
                    elapsed = now - started_at
                    if elapsed >= seconds:
                        break
                    phase, instruction = phase_at(elapsed)
                    if phase != previous_phase:
                        print(instruction, flush=True)
                        log.write(f'# phase={phase} elapsed_s={int(elapsed)}\n')
                        previous_phase = phase

                if not select.select([fd], [], [], 0.2)[0]:
                    continue
                try:
                    chunk = os.read(fd, 4096)
                except BlockingIOError:
                    continue
                if not chunk:
                    raise RuntimeError('USB接続が切れました。ここまでの集計値は保存されています。')
                pending += chunk
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    record = parse_record(line)
                    if record is None:
                        continue
                    if started_at is None:
                        started_at = time.monotonic()
                        print(f'受信を確認しました。約{seconds}秒で自動終了します。', flush=True)
                    elapsed_ms = int((time.monotonic() - started_at) * 1000)
                    log.write(f'# received_ms={elapsed_ms}\n{format_record(record)}\n')
                    log.flush()
                    count += 1
                if len(pending) > 1024:
                    pending = b''
    finally:
        if previous is not None:
            try:
                fcntl.ioctl(fd, termios.TIOCMBIC, struct.pack('I', termios.TIOCM_DTR))
                termios.tcsetattr(fd, termios.TCSANOW, previous)
            except OSError:
                pass
        os.close(fd)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=90, choices=range(15, 301), metavar='15..300')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.exit(1, 'この収集スクリプトはmacOS用です。\n')
    output = None
    try:
        port = find_port()
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        output = Path(__file__).resolve().parent / f'roba-diagnostic-{stamp}.txt'
        count = collect(port, output, args.seconds)
        print(f'完了しました。集計データ {count} 行を保存しました。')
    except KeyboardInterrupt:
        print('\n中断しました。受信済みの集計値だけ保存しています。')
        return 130
    except (OSError, RuntimeError, subprocess.SubprocessError, plistlib.InvalidFileException) as error:
        if isinstance(error, RuntimeError):
            print(str(error), file=sys.stderr)
        else:
            print('USB端子の確認・読み取り、またはファイル保存に失敗しました。', file=sys.stderr)
        return 1
    finally:
        if output is not None and output.exists():
            print(f'保存先: {output}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

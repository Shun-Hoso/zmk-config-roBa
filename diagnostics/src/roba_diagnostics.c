#include "roba_diagnostics.h"

#include <stdio.h>
#include <string.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/input/input.h>
#include <zmk/endpoints.h>

#define DIAG_FIELDS(X)                                                                             \
    X(irq) X(work) X(done) X(irq_err) X(submit_err) X(reads) X(moving) X(zero) X(read_err)            \
    X(quality_min) X(quality_max) X(queue_max_ms) X(read_max_ms) X(post) X(post_err)                  \
    X(post_max_ms) X(events) X(frames) X(filtered) X(process_max_ms) X(send) X(send_err)             \
    X(send_max_ms)

struct diagnostic_counts {
#define DECLARE_FIELD(name) uint32_t name;
    DIAG_FIELDS(DECLARE_FIELD)
#undef DECLARE_FIELD
};

struct pending_operation {
    bool active;
    uint32_t since;
};

static struct diagnostic_counts counts;
static struct k_spinlock counts_lock;
static struct pending_operation queued, working, reading, posting, processing, sending;
static bool irq_enabled;
static bool scroll_mode;

static const struct device *const trackball = DEVICE_DT_GET(DT_NODELABEL(trackball));
static const struct device *const serial = DEVICE_DT_GET(DT_NODELABEL(roba_diag_uart));
static const struct gpio_dt_spec motion_pin =
    GPIO_DT_SPEC_GET(DT_NODELABEL(trackball), irq_gpios);

static void begin(struct pending_operation *op) {
    op->active = true;
    op->since = k_uptime_get_32();
}

static uint32_t age(const struct pending_operation *op, uint32_t now) {
    return op->active ? now - op->since : 0;
}

static void finish(struct pending_operation *op, uint32_t *maximum) {
    *maximum = MAX(*maximum, age(op, k_uptime_get_32()));
    op->active = false;
}

void roba_diag_irq(void) {
    k_spinlock_key_t key = k_spin_lock(&counts_lock);
    counts.irq++;
    begin(&queued);
    k_spin_unlock(&counts_lock, key);
}

void roba_diag_irq_configured(bool enabled, int result) {
    k_spinlock_key_t key = k_spin_lock(&counts_lock);
    if (result < 0) {
        counts.irq_err++;
    } else {
        irq_enabled = enabled;
    }
    k_spin_unlock(&counts_lock, key);
}

void roba_diag_submitted(int result) {
    if (result >= 0) {
        return;
    }
    k_spinlock_key_t key = k_spin_lock(&counts_lock);
    counts.submit_err++;
    k_spin_unlock(&counts_lock, key);
}

void roba_diag_work_begin(void) {
    k_spinlock_key_t key = k_spin_lock(&counts_lock);
    counts.work++;
    finish(&queued, &counts.queue_max_ms);
    begin(&working);
    k_spin_unlock(&counts_lock, key);
}

void roba_diag_work_end(void) {
    k_spinlock_key_t key = k_spin_lock(&counts_lock);
    counts.done++;
    working.active = false;
    k_spin_unlock(&counts_lock, key);
}

void roba_diag_read_begin(void) {
    k_spinlock_key_t key = k_spin_lock(&counts_lock);
    begin(&reading);
    k_spin_unlock(&counts_lock, key);
}

void roba_diag_motion(int result, bool moving, uint8_t quality, bool scrolling) {
    k_spinlock_key_t key = k_spin_lock(&counts_lock);
    counts.reads++;
    finish(&reading, &counts.read_max_ms);
    scroll_mode = scrolling;
    if (result < 0) {
        counts.read_err++;
    } else {
        if (counts.moving + counts.zero == 0) {
            counts.quality_min = quality;
        }
        counts.quality_min = MIN(counts.quality_min, quality);
        counts.quality_max = MAX(counts.quality_max, quality);
        if (moving) {
            counts.moving++;
        } else {
            counts.zero++;
        }
    }
    k_spin_unlock(&counts_lock, key);
}

int roba_diag_report_rel(const struct device *dev, uint16_t code, int32_t value,
                         bool sync, k_timeout_t timeout) {
    k_spinlock_key_t key = k_spin_lock(&counts_lock);
    begin(&posting);
    k_spin_unlock(&counts_lock, key);

    int result = input_report_rel(dev, code, value, sync, timeout);

    key = k_spin_lock(&counts_lock);
    counts.post++;
    counts.post_err += result < 0;
    finish(&posting, &counts.post_max_ms);
    k_spin_unlock(&counts_lock, key);
    return result;
}

void roba_diag_process_begin(const struct device *dev, bool sync) {
    if (dev != trackball) {
        return;
    }
    k_spinlock_key_t key = k_spin_lock(&counts_lock);
    counts.events++;
    counts.frames += sync;
    begin(&processing);
    k_spin_unlock(&counts_lock, key);
}

void roba_diag_process_end(const struct device *dev, bool filtered) {
    if (dev != trackball) {
        return;
    }
    k_spinlock_key_t key = k_spin_lock(&counts_lock);
    counts.filtered += filtered;
    finish(&processing, &counts.process_max_ms);
    k_spin_unlock(&counts_lock, key);
}

void roba_diag_send_begin(const struct device *dev) {
    if (dev != trackball) {
        return;
    }
    k_spinlock_key_t key = k_spin_lock(&counts_lock);
    begin(&sending);
    k_spin_unlock(&counts_lock, key);
}

void roba_diag_send_end(const struct device *dev, int result) {
    if (dev != trackball) {
        return;
    }
    k_spinlock_key_t key = k_spin_lock(&counts_lock);
    counts.send++;
    counts.send_err += result < 0;
    finish(&sending, &counts.send_max_ms);
    k_spin_unlock(&counts_lock, key);
}

/* USBが読まれなくても、センサー処理を待たせない。未送信の行は次の集計で数える。 */
static struct k_spinlock tx_lock;
static char tx_buffer[1024];
static size_t tx_length, tx_offset;

static void serial_callback(const struct device *dev, void *user_data) {
    ARG_UNUSED(user_data);
    if (!uart_irq_update(dev) || !uart_irq_tx_ready(dev)) {
        return;
    }

    k_spinlock_key_t key = k_spin_lock(&tx_lock);
    int written = uart_fifo_fill(dev, (const uint8_t *)tx_buffer + tx_offset,
                                tx_length - tx_offset);
    if (written > 0) {
        tx_offset += written;
    }
    if (tx_offset == tx_length) {
        uart_irq_tx_disable(dev);
    }
    k_spin_unlock(&tx_lock, key);
}

static bool send_snapshot(const char *line, size_t length) {
    k_spinlock_key_t key = k_spin_lock(&tx_lock);
    if (tx_offset != tx_length) {
        k_spin_unlock(&tx_lock, key);
        return false;
    }
    memcpy(tx_buffer, line, length);
    tx_length = length;
    tx_offset = 0;
    k_spin_unlock(&tx_lock, key);
    uart_irq_tx_enable(serial);
    return true;
}

static void diagnostic_thread(void *a, void *b, void *c) {
    ARG_UNUSED(a);
    ARG_UNUSED(b);
    ARG_UNUSED(c);
    if (!device_is_ready(serial) || uart_irq_callback_user_data_set(serial, serial_callback, NULL)) {
        return;
    }

    uint32_t sequence = 0, last_snapshot = k_uptime_get_32(), output_dropped = 0;
    while (true) {
        k_sleep(K_SECONDS(1));
        uint32_t now = k_uptime_get_32();
        struct diagnostic_counts snapshot;
        uint32_t queue_age, work_age, read_age, post_age, process_age, send_age;
        bool enabled, scrolling;

        k_spinlock_key_t key = k_spin_lock(&counts_lock);
        snapshot = counts;
        queue_age = age(&queued, now);
        work_age = age(&working, now);
        read_age = age(&reading, now);
        post_age = age(&posting, now);
        process_age = age(&processing, now);
        send_age = age(&sending, now);
        enabled = irq_enabled;
        scrolling = scroll_mode;
        k_spin_unlock(&counts_lock, key);

        uint32_t span = now - last_snapshot;
        last_snapshot = now;
        sequence++;
        uint32_t dtr = 0;
        if (uart_line_ctrl_get(serial, UART_LINE_CTRL_DTR, &dtr) || !dtr) {
            uart_irq_tx_disable(serial);
            key = k_spin_lock(&tx_lock);
            tx_length = tx_offset = 0;
            k_spin_unlock(&tx_lock, key);
            continue;
        }

        char line[1024];
#define FORMAT_FIELD(name) " " #name "=%u"
#define VALUE_FIELD(name) , snapshot.name
        int length = snprintf(line, sizeof(line),
            "ROBA_DIAG_V1 seq=%u ms=%u span=%u usb=%u irq_on=%u irq_pin=%d scroll=%u"
            " queue_age_ms=%u work_age_ms=%u read_age_ms=%u post_age_ms=%u"
            " process_age_ms=%u send_age_ms=%u output_drop=%u"
            DIAG_FIELDS(FORMAT_FIELD)
            "\r\n", sequence, now, span,
            zmk_endpoints_selected().transport == ZMK_TRANSPORT_USB,
            enabled, gpio_pin_get_dt(&motion_pin), scrolling,
            queue_age, work_age, read_age, post_age, process_age, send_age, output_dropped
            DIAG_FIELDS(VALUE_FIELD)
        );
#undef FORMAT_FIELD
#undef VALUE_FIELD
        if (length < 0 || (size_t)length >= sizeof(line) || !send_snapshot(line, length)) {
            output_dropped++;
        }
    }
}

K_THREAD_DEFINE(roba_diagnostic_thread, 3072, diagnostic_thread, NULL, NULL, NULL, 10, 0, 0);

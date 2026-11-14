#pragma once

#include <zephyr/device.h>
#include <zephyr/kernel.h>

void roba_diag_irq(void);
void roba_diag_irq_configured(bool enabled, int result);
void roba_diag_submitted(int result);
void roba_diag_work_begin(void);
void roba_diag_work_end(void);
void roba_diag_motion(int result, int16_t x, int16_t y, uint8_t quality,
                      bool scrolling, bool pointing);
void roba_diag_read_begin(void);
int roba_diag_report_rel(const struct device *dev, uint16_t code, int32_t value,
                         bool sync, k_timeout_t timeout);
void roba_diag_process_begin(const struct device *dev, bool sync);
void roba_diag_process_end(const struct device *dev, bool filtered);
void roba_diag_send_begin(const struct device *dev, int16_t x, int16_t y, bool pointing);
void roba_diag_send_end(const struct device *dev, int result);

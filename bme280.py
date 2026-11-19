"""
Minimal I2C driver for the BME280 sensor (temperature, humidity, pressure).
Implements the compensation formulas from the Bosch BME280 datasheet.

Usage:
    from machine import I2C, Pin
    from bme280 import BME280

    i2c = I2C(0, sda=Pin(0), scl=Pin(1), freq=100000)
    temperature, pressure, humidity = BME280(i2c).read()
"""

import time

BME280_ADDR = 0x76  # some modules use 0x77 - check with i2c.scan()

REG_CTRL_HUM = 0xF2
REG_STATUS = 0xF3
REG_CTRL_MEAS = 0xF4
REG_CONFIG = 0xF5
REG_DATA = 0xF7  # 8 bytes: pressure(3) + temperature(3) + humidity(2)


class BME280:
    def __init__(self, i2c, address=BME280_ADDR):
        self.i2c = i2c
        self.addr = address
        self._load_calibration()

        # Sensor stays in sleep mode; read() triggers a single forced
        # measurement. This matters after a cold power-up (e.g. TPL5110):
        # until the first conversion finishes, the data registers hold
        # reset values that decode to a fixed, plausible-looking reading.
        self.i2c.writeto_mem(self.addr, REG_CTRL_MEAS, b'\x00')  # sleep mode
        self.i2c.writeto_mem(self.addr, REG_CONFIG, b'\x00')     # filter off
        self.i2c.writeto_mem(self.addr, REG_CTRL_HUM, b'\x01')   # humidity x1
        self.t_fine = 0

    def _measure(self, timeout_ms=100):
        """Start a forced measurement and wait until it completes."""
        # temperature x1, pressure x1, forced mode
        self.i2c.writeto_mem(self.addr, REG_CTRL_MEAS, b'\x25')
        time.sleep_ms(10)  # typical conversion time for x1/x1/x1 is ~8 ms
        start = time.ticks_ms()
        while self.i2c.readfrom_mem(self.addr, REG_STATUS, 1)[0] & 0x08:
            if time.ticks_diff(time.ticks_ms(), start) > timeout_ms:
                raise OSError("BME280 measurement timeout")
            time.sleep_ms(2)

    @staticmethod
    def _u16(data, i):
        return data[i] | (data[i + 1] << 8)

    @staticmethod
    def _s16(data, i):
        v = BME280._u16(data, i)
        return v - 65536 if v > 32767 else v

    def _load_calibration(self):
        d1 = self.i2c.readfrom_mem(self.addr, 0x88, 26)
        d2 = self.i2c.readfrom_mem(self.addr, 0xE1, 7)

        self.dig_T1 = self._u16(d1, 0)
        self.dig_T2 = self._s16(d1, 2)
        self.dig_T3 = self._s16(d1, 4)

        self.dig_P1 = self._u16(d1, 6)
        self.dig_P2 = self._s16(d1, 8)
        self.dig_P3 = self._s16(d1, 10)
        self.dig_P4 = self._s16(d1, 12)
        self.dig_P5 = self._s16(d1, 14)
        self.dig_P6 = self._s16(d1, 16)
        self.dig_P7 = self._s16(d1, 18)
        self.dig_P8 = self._s16(d1, 20)
        self.dig_P9 = self._s16(d1, 22)

        self.dig_H1 = d1[25]
        self.dig_H2 = self._s16(d2, 0)
        self.dig_H3 = d2[2]
        e4, e5, e6 = d2[3], d2[4], d2[5]
        self.dig_H4 = (e4 << 4) | (e5 & 0x0F)
        if self.dig_H4 > 2047:
            self.dig_H4 -= 4096
        self.dig_H5 = (e6 << 4) | (e5 >> 4)
        if self.dig_H5 > 2047:
            self.dig_H5 -= 4096
        self.dig_H6 = d2[6]
        if self.dig_H6 > 127:
            self.dig_H6 -= 256

    def read(self):
        """Return (temperature_C, pressure_hPa, humidity_percent)."""
        self._measure()
        data = self.i2c.readfrom_mem(self.addr, REG_DATA, 8)

        raw_press = (data[0] << 12) | (data[1] << 4) | (data[2] >> 4)
        raw_temp = (data[3] << 12) | (data[4] << 4) | (data[5] >> 4)
        raw_hum = (data[6] << 8) | data[7]

        # --- temperature ---
        var1 = (raw_temp / 16384.0 - self.dig_T1 / 1024.0) * self.dig_T2
        var2 = ((raw_temp / 131072.0 - self.dig_T1 / 8192.0) ** 2) * self.dig_T3
        self.t_fine = var1 + var2
        temperature = self.t_fine / 5120.0

        # --- pressure ---
        var1 = self.t_fine / 2.0 - 64000.0
        var2 = var1 * var1 * self.dig_P6 / 32768.0
        var2 = var2 + var1 * self.dig_P5 * 2.0
        var2 = var2 / 4.0 + self.dig_P4 * 65536.0
        var1 = (self.dig_P3 * var1 * var1 / 524288.0 + self.dig_P2 * var1) / 524288.0
        var1 = (1.0 + var1 / 32768.0) * self.dig_P1
        if var1 == 0:
            pressure = 0.0
        else:
            p = 1048576.0 - raw_press
            p = (p - var2 / 4096.0) * 6250.0 / var1
            var1 = self.dig_P9 * p * p / 2147483648.0
            var2 = p * self.dig_P8 / 32768.0
            pressure = (p + (var1 + var2 + self.dig_P7) / 16.0) / 100.0

        # --- humidity ---
        h = self.t_fine - 76800.0
        h = ((raw_hum - (self.dig_H4 * 64.0 + self.dig_H5 / 16384.0 * h)) *
             (self.dig_H2 / 65536.0 * (1.0 + self.dig_H6 / 67108864.0 * h *
              (1.0 + self.dig_H3 / 67108864.0 * h))))
        h = h * (1.0 - self.dig_H1 * h / 524288.0)
        humidity = max(0.0, min(100.0, h))

        return temperature, pressure, humidity

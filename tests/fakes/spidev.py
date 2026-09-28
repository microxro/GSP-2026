"""Fake ``spidev`` module for tests (no real SPI hardware / spidev package)."""


class SpiDev:
    # Class-level so tests can inspect every instance created by Leds().
    instances = []
    fail_open = False
    fail_xfer = False

    def __init__(self):
        self.opened = None
        self.max_speed_hz = None
        self.transfers = []
        SpiDev.instances.append(self)

    def open(self, bus, device):
        if SpiDev.fail_open:
            raise OSError("fake spidev: could not open SPI device")
        self.opened = (bus, device)

    def xfer2(self, data):
        if SpiDev.fail_xfer:
            raise OSError("fake spidev: xfer2 failed")
        self.transfers.append(list(data))
        return list(data)

    def close(self):
        pass

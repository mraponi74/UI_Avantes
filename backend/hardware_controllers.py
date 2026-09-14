"""
Avantes spectrometer control over USB (libavs.so / AvaSpec SDK).
Acquisition only — no motor, no shutter, no GPIO.
"""

import os
import time
import threading
import numpy as np
import logging
from pathlib import Path
from typing import Optional, Tuple

from avaspec import (
    AVS_Init, AVS_GetList, AVS_Activate, AVS_UseHighResAdc,
    AVS_PrepareMeasure, AVS_Measure, AVS_PollScan, AVS_GetScopeData,
    AVS_StopMeasure, AVS_Deactivate, AVS_Done, MeasConfigType,
)
from calibration import CalibrationStore

logger = logging.getLogger(__name__)

# Persisted in the /data volume so calibrations survive container recreation.
CALIB_PATH = Path(os.environ.get("CALIB_PATH", "/data/calibrations.json"))
_calib_store = CalibrationStore(CALIB_PATH)

NUM_PIXELS = 2048
AVANTES_USB_VENDOR_ID = "1992"


def _read_usb_model_name() -> Optional[str]:
    """
    Reads the USB product string (e.g. "AS7010") for the connected Avantes
    device straight from the kernel's USB descriptors. The AvaSpec SDK's own
    identity struct doesn't expose the model — its "friendly name" defaults
    to the serial number unless someone set a custom one via Avantes' own
    software.
    """
    sysfs_root = Path("/sys/bus/usb/devices")
    try:
        for dev_dir in sysfs_root.iterdir():
            vendor_file = dev_dir / "idVendor"
            if not vendor_file.is_file():
                continue
            if vendor_file.read_text().strip() != AVANTES_USB_VENDOR_ID:
                continue
            product_file = dev_dir / "product"
            if product_file.is_file():
                name = product_file.read_text().strip()
                if name:
                    return name
    except Exception as e:
        logger.warning(f"Could not read USB model name from sysfs: {e}")
    return None


class SpectrometerController:
    """Avantes spectrometer controller"""

    RECONNECT_INTERVAL_S = 10   # seconds between auto-reconnect attempts
    CONNECT_RETRIES      = 5    # attempts on startup before spawning the retry thread

    # Fallback crop range used until a real calibration is known (full pixel range).
    DEFAULT_WAVE_MIN = 0.0
    DEFAULT_WAVE_MAX = float(NUM_PIXELS - 1)

    def __init__(self):
        self.dev_handle = None
        self.is_connected = False
        self.measconfig = None
        self._stop_reconnect = threading.Event()
        self._reconnect_thread = None

        self.serial_number: Optional[str] = None
        self.model_name: Optional[str] = None
        self.needs_calibration = False
        self.wavelengths = np.arange(NUM_PIXELS, dtype=float)
        self.WAVE_MIN = self.DEFAULT_WAVE_MIN
        self.WAVE_MAX = self.DEFAULT_WAVE_MAX

        self._connect()
        if not self.is_connected:
            self._start_reconnect_thread()

    def _start_reconnect_thread(self):
        self._stop_reconnect.clear()
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, daemon=True, name="spec-reconnect"
        )
        self._reconnect_thread.start()
        logger.info(f"Auto-reconnect active: retrying every {self.RECONNECT_INTERVAL_S}s")

    def _reconnect_loop(self):
        while not self._stop_reconnect.is_set():
            self._stop_reconnect.wait(self.RECONNECT_INTERVAL_S)
            if self._stop_reconnect.is_set():
                break
            if not self.is_connected:
                logger.info("Auto-reconnect: trying to connect spectrometer...")
                self._connect()
                if self.is_connected:
                    logger.info("Auto-reconnect successful")
                    break

    def _apply_calibration(self, calib: dict):
        c0 = calib["c0"]; c1 = calib["c1"]; c2 = calib["c2"]; c3 = calib["c3"]
        self.wavelengths = np.array([
            c0 + c1 * i + c2 * i**2 + c3 * i**3 for i in range(NUM_PIXELS)
        ])
        self.WAVE_MIN = calib.get("wave_min", float(self.wavelengths.min()))
        self.WAVE_MAX = calib.get("wave_max", float(self.wavelengths.max()))
        # El modelo real (ej: "AvaSpec-ULS2048XL-EVO") no lo expone ni el SDK
        # ni el descriptor USB — sólo el chip controlador (ej: "AS7010"). Si
        # la calibración lo trae guardado, lo preferimos sobre ese fallback.
        if calib.get("model"):
            self.model_name = calib["model"]

    def set_calibration(self, calib: dict):
        """Saves and immediately applies a wavelength calibration for the
        currently connected spectrometer's serial number."""
        if not self.serial_number:
            raise RuntimeError("No spectrometer connected")
        _calib_store.set(self.serial_number, calib)
        self._apply_calibration(calib)
        self.needs_calibration = False
        logger.info(f"Calibration applied for serial {self.serial_number}")

    def _connect(self):
        # Clear previous SDK state
        try:
            AVS_Done()
        except Exception:
            pass
        time.sleep(0.5)

        # Retry AVS_Init up to CONNECT_RETRIES times
        n = 0
        for attempt in range(self.CONNECT_RETRIES):
            try:
                n = AVS_Init(0)
            except Exception as e:
                logger.warning(f"AVS_Init exception on attempt {attempt+1}: {e}")
                n = 0
            if n > 0:
                break
            if attempt < self.CONNECT_RETRIES - 1:
                logger.warning(f"AVS_Init={n}, retrying ({attempt+1}/{self.CONNECT_RETRIES})...")
                time.sleep(2.0)

        logger.info(f"--> Detected {n} spectrometer(s)")
        if n <= 0:
            logger.error("--> No spectrometer detected")
            return

        try:
            _, ids = AVS_GetList()
            serial = ids[0].SerialNumber
            self.serial_number = (serial.decode("ascii", errors="ignore") if isinstance(serial, bytes) else str(serial)).strip()
            self.model_name = _read_usb_model_name()
            logger.info(f"Device identity — serial: {self.serial_number!r}  model: {self.model_name!r}")

            self.dev_handle = AVS_Activate(ids[0])
            AVS_UseHighResAdc(self.dev_handle, True)

            self.measconfig = MeasConfigType()
            self.measconfig.m_StartPixel              = 0
            self.measconfig.m_StopPixel               = NUM_PIXELS - 1
            self.measconfig.m_IntegrationTime         = 100.0
            self.measconfig.m_IntegrationDelay        = 0
            self.measconfig.m_NrAverages              = 1
            self.measconfig.m_CorDynDark_m_Enable     = 0
            self.measconfig.m_CorDynDark_m_ForgetPercentage = 0
            self.measconfig.m_Smoothing_m_SmoothPix   = 0
            self.measconfig.m_Smoothing_m_SmoothModel = 0
            self.measconfig.m_SaturationDetection     = 0
            self.measconfig.m_Trigger_m_Mode          = 0
            self.measconfig.m_Trigger_m_Source        = 0
            self.measconfig.m_Trigger_m_SourceType    = 0
            self.measconfig.m_Control_m_StrobeControl = 0
            self.measconfig.m_Control_m_LaserDelay    = 0
            self.measconfig.m_Control_m_LaserWidth    = 0
            self.measconfig.m_Control_m_LaserWaveLength = 0.0
            self.measconfig.m_Control_m_StoreToRam    = 0

            AVS_PrepareMeasure(self.dev_handle, self.measconfig)
            self.is_connected = True
            logger.info(f"Spectrometer connected and configured (serial {self.serial_number})")

            calib = _calib_store.get(self.serial_number)
            if calib:
                self._apply_calibration(calib)
                self.needs_calibration = False
                logger.info(f"Loaded known calibration for serial {self.serial_number}")
            else:
                self.needs_calibration = True
                self.wavelengths = np.arange(NUM_PIXELS, dtype=float)
                self.WAVE_MIN = self.DEFAULT_WAVE_MIN
                self.WAVE_MAX = self.DEFAULT_WAVE_MAX
                logger.warning(
                    f"No calibration known for serial {self.serial_number} — "
                    f"showing raw pixel index until calibration data is entered"
                )

        except Exception as e:
            logger.error(f"Error connecting spectrometer: {e}")

    def set_integration_time(self, time_ms: float):
        if not self.is_connected:
            logger.warning("Spectrometer not connected")
            return
        self.measconfig.m_IntegrationTime = float(time_ms)
        AVS_StopMeasure(self.dev_handle)
        ret = AVS_PrepareMeasure(self.dev_handle, self.measconfig)
        if ret != 0:
            logger.error(f"Error in AVS_PrepareMeasure: {ret}")
        else:
            logger.info(f"--> Integration time: {time_ms} ms")

    def _crop(self, wl: np.ndarray, spec: np.ndarray):
        mask = (wl >= self.WAVE_MIN) & (wl <= self.WAVE_MAX)
        return wl[mask], spec[mask]

    def get_spectrum(self, num_avg: int = 1, filter_size: int = 1) -> Tuple[np.ndarray, np.ndarray]:
        if not self.is_connected:
            logger.warning("Spectrometer not connected. Simulating output")
            return self._crop(self.wavelengths, np.random.rand(NUM_PIXELS) * 1000 + 500)

        try:
            acum = []
            for _ in range(num_avg):
                AVS_Measure(self.dev_handle, 0, 1)
                deadline = time.time() + max(self.measconfig.m_IntegrationTime / 1000 * 10, 5.0)
                while not AVS_PollScan(self.dev_handle):
                    time.sleep(0.001)
                    if time.time() > deadline:
                        logger.error("AVS_PollScan timeout — SDK hung, aborting")
                        AVS_StopMeasure(self.dev_handle)
                        return self.wavelengths, np.zeros(NUM_PIXELS)
                _, spec = AVS_GetScopeData(self.dev_handle)
                acum.append(spec[:NUM_PIXELS])

            avg_spectrum = np.mean(acum, axis=0)

            if filter_size > 1:
                filt = np.ones(filter_size) / filter_size
                avg_spectrum = np.convolve(avg_spectrum, filt, 'same')

            return self._crop(self.wavelengths, avg_spectrum)

        except Exception as e:
            logger.error(f"Error acquiring spectrum: {e}")
            return self._crop(self.wavelengths, np.zeros(NUM_PIXELS))

    def reconnect(self):
        """Cleanly disconnects and reconnects. Call via asyncio.to_thread."""
        logger.info("Reconnecting spectrometer...")
        self._stop_reconnect.set()   # stop the auto-reconnect thread if running
        self.disconnect()
        time.sleep(1.0)
        self._connect()
        if not self.is_connected:
            self._start_reconnect_thread()

    def disconnect(self):
        self._stop_reconnect.set()
        if self.dev_handle:
            try:
                AVS_StopMeasure(self.dev_handle)
            except Exception:
                pass
            try:
                AVS_Deactivate(self.dev_handle)
            except Exception:
                pass
        try:
            AVS_Done()
        except Exception:
            pass
        self.dev_handle = None
        self.is_connected = False
        logger.info("Spectrometer disconnected")

    def __del__(self):
        self.disconnect()

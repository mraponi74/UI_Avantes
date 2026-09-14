"""
Control del espectrómetro Avantes vía USB (libavs.so / AvaSpec SDK).
Sólo adquisición de espectros — sin motor, sin shutter, sin GPIO.
"""

import time
import threading
import numpy as np
import logging
from typing import Tuple

from avaspec import (
    AVS_Init, AVS_GetList, AVS_Activate, AVS_UseHighResAdc,
    AVS_PrepareMeasure, AVS_Measure, AVS_PollScan, AVS_GetScopeData,
    AVS_StopMeasure, AVS_Deactivate, AVS_Done, MeasConfigType,
)

logger = logging.getLogger(__name__)


class SpectrometerController:
    """Controlador del espectrómetro Avantes"""

    RECONNECT_INTERVAL_S = 10   # segundos entre intentos de auto-reconexión
    CONNECT_RETRIES      = 5    # intentos al inicio antes de lanzar thread

    # Calibración de longitud de onda (polinomio propio del instrumento)
    _WL_INTERCEPT = 2.800967184e2
    _WL_C1 =  1.429352953e-1
    _WL_C2 = -5.392709165e-6
    _WL_C3 = -6.309091154e-10

    # Recorte del rango espectral útil (elimina artefactos de borde del filtro)
    WAVE_MIN = 285.0   # nm
    WAVE_MAX = 540.0   # nm

    def __init__(self):
        self.dev_handle = None
        self.is_connected = False
        self.measconfig = None
        self._stop_reconnect = threading.Event()
        self._reconnect_thread = None

        self.wavelengths = np.array([
            self._WL_INTERCEPT + self._WL_C1 * i + self._WL_C2 * i**2 + self._WL_C3 * i**3
            for i in range(2048)
        ])

        self._connect()
        if not self.is_connected:
            self._start_reconnect_thread()

    def _start_reconnect_thread(self):
        self._stop_reconnect.clear()
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, daemon=True, name="spec-reconnect"
        )
        self._reconnect_thread.start()
        logger.info(f"Auto-reconexión activa: reintento cada {self.RECONNECT_INTERVAL_S}s")

    def _reconnect_loop(self):
        while not self._stop_reconnect.is_set():
            self._stop_reconnect.wait(self.RECONNECT_INTERVAL_S)
            if self._stop_reconnect.is_set():
                break
            if not self.is_connected:
                logger.info("Auto-reconexión: intentando conectar espectrómetro...")
                self._connect()
                if self.is_connected:
                    logger.info("Auto-reconexión exitosa")
                    break

    def _connect(self):
        # Limpiar estado anterior del SDK
        try:
            AVS_Done()
        except Exception:
            pass
        time.sleep(0.5)

        # Reintentar AVS_Init hasta CONNECT_RETRIES veces
        n = 0
        for attempt in range(self.CONNECT_RETRIES):
            try:
                n = AVS_Init(0)
            except Exception as e:
                logger.warning(f"AVS_Init excepción intento {attempt+1}: {e}")
                n = 0
            if n > 0:
                break
            if attempt < self.CONNECT_RETRIES - 1:
                logger.warning(f"AVS_Init={n}, reintentando ({attempt+1}/{self.CONNECT_RETRIES})...")
                time.sleep(2.0)

        logger.info(f"--> Detectó {n} espectrómetro(s)")
        if n <= 0:
            logger.error("--> No se detectó espectrómetro")
            return

        try:
            _, ids = AVS_GetList()
            self.dev_handle = AVS_Activate(ids[0])
            AVS_UseHighResAdc(self.dev_handle, True)

            self.measconfig = MeasConfigType()
            self.measconfig.m_StartPixel              = 0
            self.measconfig.m_StopPixel               = 2047
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
            logger.info("Espectrómetro conectado y configurado")

        except Exception as e:
            logger.error(f"Error conectando espectrómetro: {e}")

    def set_integration_time(self, time_ms: float):
        if not self.is_connected:
            logger.warning("Espectrómetro no conectado")
            return
        self.measconfig.m_IntegrationTime = float(time_ms)
        AVS_StopMeasure(self.dev_handle)
        ret = AVS_PrepareMeasure(self.dev_handle, self.measconfig)
        if ret != 0:
            logger.error(f"Error en AVS_PrepareMeasure: {ret}")
        else:
            logger.info(f"--> Tiempo de integración: {time_ms} ms")

    def _crop(self, wl: np.ndarray, spec: np.ndarray):
        mask = (wl >= self.WAVE_MIN) & (wl <= self.WAVE_MAX)
        return wl[mask], spec[mask]

    def get_spectrum(self, num_avg: int = 1, filter_size: int = 1) -> Tuple[np.ndarray, np.ndarray]:
        if not self.is_connected:
            logger.warning("Espectrómetro no conectado. Simulando salida")
            return self._crop(self.wavelengths, np.random.rand(2048) * 1000 + 500)

        try:
            acum = []
            for _ in range(num_avg):
                AVS_Measure(self.dev_handle, 0, 1)
                deadline = time.time() + max(self.measconfig.m_IntegrationTime / 1000 * 10, 5.0)
                while not AVS_PollScan(self.dev_handle):
                    time.sleep(0.001)
                    if time.time() > deadline:
                        logger.error("AVS_PollScan timeout — SDK colgado, abortando")
                        AVS_StopMeasure(self.dev_handle)
                        return self.wavelengths, np.zeros(2048)
                _, spec = AVS_GetScopeData(self.dev_handle)
                acum.append(spec[:2048])

            avg_spectrum = np.mean(acum, axis=0)

            if filter_size > 1:
                filt = np.ones(filter_size) / filter_size
                avg_spectrum = np.convolve(avg_spectrum, filt, 'same')

            return self._crop(self.wavelengths, avg_spectrum)

        except Exception as e:
            logger.error(f"Error adquiriendo espectro: {e}")
            return self._crop(self.wavelengths, np.zeros(2048))

    def reconnect(self):
        """Desconecta limpiamente y vuelve a conectar. Llamar con asyncio.to_thread."""
        logger.info("Reconectando espectrómetro...")
        self._stop_reconnect.set()   # detener thread de auto-reconexión si está corriendo
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
        logger.info("Espectrómetro desconectado")

    def __del__(self):
        self.disconnect()

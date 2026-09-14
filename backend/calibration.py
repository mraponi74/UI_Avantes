"""
Almacén de calibraciones de longitud de onda por número de serie de
espectrómetro Avantes. Cada unidad tiene su propia calibración de fábrica
(polinomio de 3er grado); acá se persiste la calibración medida a mano para
cada equipo usado, para no tener que remedirla cada vez que se cambia de
unidad.
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class CalibrationStore:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except Exception as e:
                logger.error(f"Error loading calibration store {self.path}: {e}")
                self._data = {}

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=2))
        except Exception as e:
            logger.error(f"Error saving calibration store {self.path}: {e}")

    def get(self, serial: str) -> Optional[dict]:
        return self._data.get(serial)

    def set(self, serial: str, calib: dict):
        self._data[serial] = calib
        self._save()
        logger.info(f"Calibration saved for serial {serial}")

    def known_serials(self) -> list:
        return list(self._data.keys())

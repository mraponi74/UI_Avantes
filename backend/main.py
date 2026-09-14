"""
Backend de adquisición de espectros para espectrómetro Avantes (AvaSpec SDK).
Sólo adquisición: tiempo de integración, delay entre espectros, promediación
y filtro. Sin control de motor ni de shutter.
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from pydantic import BaseModel
from typing import Optional, List
import asyncio
import json
import numpy as np
import os
from collections import deque
from datetime import datetime
from pathlib import Path
import logging

Q_MAX = 65535.0   # ADC 16-bit (AVS_UseHighResAdc activado en hardware_controllers.py)

from hardware_controllers import SpectrometerController

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# =============================================================================
# LOG BUFFER — para mostrar en vivo lo que hace el backend en el frontend
# =============================================================================

class _LogBufferHandler(logging.Handler):
    """Guarda los últimos logs de la app (no de uvicorn.access) en memoria."""

    def __init__(self, maxlen: int = 500):
        super().__init__()
        self.buffer: deque = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord):
        if record.name.startswith("uvicorn"):
            return
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        self.buffer.append({
            "t": datetime.now().strftime("%H:%M:%S"),
            "level": record.levelname,
            "msg": msg,
        })

_log_handler = _LogBufferHandler()
_log_handler.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(_log_handler)


# =============================================================================
# MODELOS DE DATOS
# =============================================================================

class SpecParameters(BaseModel):
    ti_ms: float             # tiempo de integración (ms)
    delay_s: float           # delay entre espectros en modo continuo (s)
    num_averages: int        # cantidad de espectros promediados por medición
    filter_size: int = 1     # tamaño del filtro de suavizado (media móvil, en px)

class MetadataParameters(BaseModel):
    save_path: str
    sample_name: str = ""    # nombre de la muestra/sitio (opcional, va en el nombre del archivo)

class SystemConfig(BaseModel):
    mode: str                # "SINGLE" | "CONTINUOUS"
    spec_params: SpecParameters
    metadata: MetadataParameters

class SystemStatus(BaseModel):
    is_running: bool
    is_recording: bool
    current_mode: Optional[str]
    spectrometer_connected: bool
    spectrometer_serial: Optional[str]
    needs_calibration: bool
    last_error: Optional[str]
    buffer_count: int   # espectros grabados, pendientes de guardar

class CalibrationInput(BaseModel):
    c0: float   # intercept (nm)
    c1: float   # linear term (nm/px)
    c2: float   # quadratic term
    c3: float   # cubic term
    wave_min: Optional[float] = None   # crop range (nm); omit to keep full range
    wave_max: Optional[float] = None


# =============================================================================
# ESTADO GLOBAL
# =============================================================================

class GlobalState:
    def __init__(self):
        self.is_running       = False
        self.is_recording     = False   # si True, los espectros adquiridos se acumulan para guardar
        self.current_mode     = None
        self.config: Optional[SystemConfig] = None
        self.active_websockets: List[WebSocket] = []
        self.spectrum_count   = 0
        self.last_error       = None
        self.acquisition_task: Optional[asyncio.Task] = None
        self.spectra_buffer: list = []   # espectros acumulados durante la grabación actual
        self.acquisition_start_time: Optional[datetime] = None

        self.spectrometer = None   # se asigna en lifespan startup

state = GlobalState()


# =============================================================================
# LIFESPAN (startup / shutdown)
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        state.spectrometer = SpectrometerController()
        logger.info("Spectrometer initialized")
    except Exception as e:
        logger.error(f"Error initializing spectrometer: {e}")

    yield

    try:
        state.spectrometer.disconnect()
    except Exception as e:
        logger.error(f"Error disconnecting spectrometer: {e}")


# =============================================================================
# APP
# =============================================================================

app = FastAPI(title="Avantes Spectrometer Acquisition API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# ENDPOINTS REST
# =============================================================================

@app.get("/api/status")
async def get_status() -> SystemStatus:
    return SystemStatus(
        is_running=state.is_running,
        is_recording=state.is_recording,
        current_mode=state.current_mode,
        spectrometer_connected=state.spectrometer.is_connected if state.spectrometer else False,
        spectrometer_serial=state.spectrometer.serial_number if state.spectrometer else None,
        needs_calibration=state.spectrometer.needs_calibration if state.spectrometer else False,
        last_error=state.last_error,
        buffer_count=len(state.spectra_buffer),
    )

@app.get("/api/spectrometer/calibration")
async def get_calibration():
    if not state.spectrometer:
        return {"connected": False}
    return {
        "connected": state.spectrometer.is_connected,
        "serial": state.spectrometer.serial_number,
        "needs_calibration": state.spectrometer.needs_calibration,
        "wave_min": state.spectrometer.WAVE_MIN,
        "wave_max": state.spectrometer.WAVE_MAX,
    }

@app.post("/api/spectrometer/calibration")
async def set_calibration(calib: CalibrationInput):
    if not state.spectrometer or not state.spectrometer.is_connected:
        return {"status": "error", "message": "No spectrometer connected"}
    try:
        data = calib.model_dump(exclude_none=True)
        state.spectrometer.set_calibration(data)
        return {
            "status": "ok",
            "message": f"Calibration saved for serial {state.spectrometer.serial_number}",
            "wave_min": state.spectrometer.WAVE_MIN,
            "wave_max": state.spectrometer.WAVE_MAX,
        }
    except Exception as e:
        logger.error(f"Error saving calibration: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/api/config")
async def set_config(config: SystemConfig):
    if state.is_running:
        msg = "Cannot change configuration while acquisition is running"
        logger.warning(msg)
        return {"status": "error", "message": msg}

    state.config = config
    logger.info(f"Configuration updated: mode={config.mode} ti={config.spec_params.ti_ms}ms")

    try:
        state.spectrometer.set_integration_time(config.spec_params.ti_ms)
    except Exception as e:
        logger.error(f"Error configuring spectrometer: {e}")

    return {"status": "ok", "message": "Configuration applied"}

@app.post("/api/start")
async def start_acquisition():
    if state.is_running:
        logger.warning("START ignored: already running")
        return {"status": "error", "message": "Already running"}
    if not state.config:
        logger.warning("START ignored: no configuration loaded")
        return {"status": "error", "message": "No configuration loaded"}

    state.is_running     = True
    state.current_mode   = state.config.mode
    state.spectrum_count = 0
    state.last_error      = None
    _log_handler.buffer.clear()
    state.acquisition_task = asyncio.create_task(acquisition_loop())
    logger.info(f"Acquisition started: {state.current_mode}")
    return {"status": "ok", "message": "Acquisition started"}

@app.post("/api/stop")
async def stop_acquisition():
    if not state.is_running:
        logger.warning("STOP ignored: not running")
        return {"status": "error", "message": "Not running"}
    state.is_running = False
    if state.acquisition_task and not state.acquisition_task.done():
        state.acquisition_task.cancel()
        try:
            await state.acquisition_task
        except (asyncio.CancelledError, Exception):
            pass
    logger.info("Acquisition stopped")
    return {"status": "ok", "message": "Acquisition stopped"}

@app.post("/api/record/start")
async def record_start():
    state.is_recording = True
    state.spectra_buffer = []
    state.acquisition_start_time = datetime.now()
    logger.info("Recording started")
    return {"status": "ok", "message": "Recording started"}

@app.post("/api/record/stop")
async def record_stop():
    state.is_recording = False
    n = len(state.spectra_buffer)
    logger.info(f"Recording stopped: {n} spectrum/spectra accumulated")
    return {"status": "ok", "message": f"Recording stopped ({n} spectra)"}

@app.post("/api/save")
async def save_data():
    if not state.config:
        logger.warning("SAVE ignored: no configuration loaded")
        return {"status": "error", "message": "No configuration loaded"}
    if not state.spectra_buffer:
        logger.warning("SAVE ignored: no data to save")
        return {"status": "error", "message": "No data to save"}
    try:
        t = state.acquisition_start_time or datetime.now()
        result = await asyncio.to_thread(_write_file, list(state.spectra_buffer), t)
        state.spectra_buffer = []
        return result
    except Exception as e:
        logger.error(f"Save error: {e}")
        return {"status": "error", "message": str(e)}


def _write_file(buf: list, start_time: datetime) -> dict:
    """
    Escribe un archivo por adquisición.
    Nombre: spec_{sample}_{YYYYMMDD}_{HHMMSS}.txt
    Carpeta: {save_path}/{YYYY-MM-DD}/
    Columnas: wl | spec_1 | spec_2 | ...
    """
    cfg  = state.config
    meta = cfg.metadata
    wl   = buf[0]['wavelengths']

    day_str      = start_time.strftime('%Y-%m-%d')
    date_compact = start_time.strftime('%Y%m%d')
    time_str     = start_time.strftime('%H%M%S')

    sample  = (meta.sample_name or 'sample').strip().replace(' ', '_')
    day_dir = Path(meta.save_path) / day_str
    day_dir.mkdir(parents=True, exist_ok=True)
    out_file = day_dir / f"spec_{sample}_{date_compact}_{time_str}.txt"

    ti_list = [round(s['metadata'].get('ti_ms', 0), 1) for s in buf]
    ti_str  = str(ti_list[0]) if len(set(ti_list)) == 1 else str(ti_list)

    header = [
        "# Avantes Spectrometer Acquisition",
        f"# Sample:         {meta.sample_name or 'N/A'}",
        f"# Date:           {day_str}",
        f"# Start_time:     {start_time.strftime('%H:%M:%S')}",
        f"# Ti_ms:          {ti_str}",
        f"# Lambda_min_nm:  {round(wl[0], 2)}",
        f"# Lambda_max_nm:  {round(wl[-1], 2)}",
        f"# Averages:       {cfg.spec_params.num_averages}",
        f"# Filter_px:      {cfg.spec_params.filter_size}",
        f"# N_spectra:      {len(buf)}",
        "#",
    ]

    col_header = ["wl"] + [f"spec_{i+1}" for i in range(len(buf))]

    with open(out_file, 'w') as f:
        f.write('\n'.join(header) + '\n')
        f.write('\t'.join(col_header) + '\n')
        for i, wl_val in enumerate(wl):
            row = [f"{wl_val:.4f}"] + [f"{s['intensities'][i]:.2f}" for s in buf]
            f.write('\t'.join(row) + '\n')

    msg = f"Saved: {day_str}/{out_file.name}  ({len(buf)} spectra)"
    logger.info(msg)
    return {"status": "ok", "message": msg, "path": str(out_file)}


@app.post("/api/spectrometer/reconnect")
async def spectrometer_reconnect():
    if state.is_running:
        return {"status": "error", "message": "Stop the acquisition before reconnecting"}
    try:
        await asyncio.to_thread(state.spectrometer.reconnect)
        connected = state.spectrometer.is_connected
        msg = "Spectrometer reconnected" if connected else "Spectrometer not detected (check USB)"
        logger.info(msg)
        return {"status": "ok" if connected else "error", "connected": connected, "message": msg}
    except Exception as e:
        logger.error(f"Reconnect error: {e}")
        return {"status": "error", "message": str(e)}


# =============================================================================
# WEBSOCKET
# =============================================================================

@app.websocket("/ws/spectrum")
async def websocket_spectrum(websocket: WebSocket):
    await websocket.accept()
    state.active_websockets.append(websocket)
    logger.info(f"WebSocket connected. Total: {len(state.active_websockets)}")

    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                if data == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        if websocket in state.active_websockets:
            state.active_websockets.remove(websocket)


# =============================================================================
# FUNCIONES DE ADQUISICIÓN
# =============================================================================

async def broadcast_spectrum(wavelengths: List[float], intensities: List[float], metadata: dict):
    if state.is_recording:
        entry = {'wavelengths': wavelengths, 'intensities': intensities, 'metadata': metadata}
        state.spectra_buffer.append(entry)
        logger.info(f"Recording: spectrum #{len(state.spectra_buffer)} accumulated")

    if not state.active_websockets:
        return

    peak_counts = int(max(intensities)) if intensities else 0
    sat_pct     = round(peak_counts / Q_MAX * 100, 1)

    message = {
        "type": "spectrum",
        "timestamp": datetime.now().isoformat(),
        "wavelengths": wavelengths,
        "intensities": intensities,
        "metadata": metadata,
        "peak_counts": peak_counts,
        "sat_pct":     sat_pct,
    }

    disconnected = []
    for ws in state.active_websockets:
        try:
            await ws.send_json(message)
        except Exception as e:
            logger.error(f"Error sending to WebSocket: {e}")
            disconnected.append(ws)
    for ws in disconnected:
        state.active_websockets.remove(ws)


async def acquire_spectrum(params: SpecParameters) -> tuple:
    wavelengths, intensities = await asyncio.to_thread(
        state.spectrometer.get_spectrum, params.num_averages, params.filter_size
    )
    return wavelengths.tolist(), intensities.tolist()


async def acquisition_loop():
    config = state.config
    params = config.spec_params
    logger.info(f"Acquisition loop started: {config.mode}")

    try:
        if config.mode == "SINGLE":
            wavelengths, intensities = await acquire_spectrum(params)
            await broadcast_spectrum(wavelengths, intensities, {
                "mode": "SINGLE", "ti_ms": params.ti_ms,
                "num_averages": params.num_averages, "spectrum_number": 0,
            })
            state.spectrum_count += 1
        elif config.mode == "CONTINUOUS":
            while state.is_running:
                wavelengths, intensities = await acquire_spectrum(params)
                await broadcast_spectrum(wavelengths, intensities, {
                    "mode": "CONTINUOUS", "ti_ms": params.ti_ms,
                    "num_averages": params.num_averages,
                    "spectrum_number": state.spectrum_count,
                })
                state.spectrum_count += 1
                await asyncio.sleep(params.delay_s)
    except Exception as e:
        logger.error(f"Acquisition error: {e}")
        state.last_error = str(e)
    finally:
        state.is_running = False
        logger.info("Acquisition loop finished")


# =============================================================================
# FRONTEND ESTÁTICO Y VISOR DE ARCHIVOS
# =============================================================================

BACKEND_DIR  = Path(__file__).parent
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"
# /data es el punto de montaje del volumen Docker (ver Dockerfile/docker-compose.yml)
# y también el save_path por defecto que ofrece el frontend.
DATA_DIR     = Path(os.environ.get("DATA_DIR", "/data"))

@app.get("/api/info")
async def get_info():
    return {"default_data_path": str(DATA_DIR)}

@app.get("/api/logs")
async def get_logs():
    return {"logs": list(_log_handler.buffer)}

@app.get("/api/viewer/folders")
async def viewer_folders():
    if not DATA_DIR.exists():
        return {"folders": []}
    folders = sorted(
        [d.name for d in DATA_DIR.iterdir() if d.is_dir()],
        reverse=True
    )
    return {"folders": folders}

@app.get("/api/viewer/files")
async def viewer_files(folder: str):
    folder_path = DATA_DIR / Path(folder).name   # sólo un nivel — sin path traversal
    if not folder_path.is_dir():
        return {"files": []}
    files = sorted(f.name for f in folder_path.glob("spec_*.txt"))
    if not files:
        files = sorted(f.name for f in folder_path.glob("*.txt"))
    return {"files": files}

@app.delete("/api/viewer/file")
async def viewer_delete_file(folder: str, filename: str):
    path = DATA_DIR / Path(folder).name / Path(filename).name
    if not path.exists() or not path.is_file():
        return {"status": "error", "message": "File not found"}
    try:
        path.unlink()
        logger.info(f"File deleted: {folder}/{filename}")
        # remove the day folder too if it's now empty
        folder_path = path.parent
        if folder_path.is_dir() and not any(folder_path.iterdir()):
            folder_path.rmdir()
        return {"status": "ok", "message": f"Deleted: {filename}"}
    except Exception as e:
        logger.error(f"Error deleting {filename}: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/api/viewer/file")
async def viewer_file(folder: str, filename: str):
    path = DATA_DIR / Path(folder).name / Path(filename).name
    if not path.exists():
        return {"error": "File not found"}
    meta: dict = {}
    col_names   = None
    rows: list  = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n\r")
            if line.startswith("#"):
                s = line[1:].strip()
                if ":" in s:
                    k, _, v = s.partition(":")
                    meta[k.strip()] = v.strip()
            elif col_names is None and line.strip():
                col_names = line.split("\t")
            elif line.strip():
                try:
                    rows.append([float(x) for x in line.split("\t")])
                except ValueError:
                    pass
    if not col_names or not rows:
        return {"error": "Could not read file"}
    arr = np.array(rows)
    spectra = {}
    for i, name in enumerate(col_names[1:], 1):
        if i >= arr.shape[1]:
            continue
        spectra[name] = arr[:, i].tolist()
    return {"meta": meta, "wavelengths": arr[:, 0].tolist(), "spectra": spectra}

class NoCacheStaticFiles(StaticFiles):
    """Evita que el navegador cachee el HTML/JS del frontend entre despliegues."""
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response

if not FRONTEND_DIR.exists():
    logger.warning(f"Frontend directory not found at {FRONTEND_DIR}")

    @app.get("/")
    async def root():
        return {"error": "Frontend not found", "expected_path": str(FRONTEND_DIR)}
else:
    app.mount("/", NoCacheStaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
    logger.info(f"Serving frontend from {FRONTEND_DIR}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")

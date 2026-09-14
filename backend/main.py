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
from datetime import datetime
from pathlib import Path
import logging

Q_MAX = 65535.0   # ADC 16-bit (AVS_UseHighResAdc activado en hardware_controllers.py)

from hardware_controllers import SpectrometerController

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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
    current_mode: Optional[str]
    spectrometer_connected: bool
    last_error: Optional[str]
    spectrum_count: int


# =============================================================================
# ESTADO GLOBAL
# =============================================================================

class GlobalState:
    def __init__(self):
        self.is_running       = False
        self.current_mode     = None
        self.config: Optional[SystemConfig] = None
        self.active_websockets: List[WebSocket] = []
        self.spectrum_count   = 0
        self.last_error       = None
        self.acquisition_task: Optional[asyncio.Task] = None
        self.spectra_buffer: list = []   # espectros de la adquisición actual
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
        logger.info("Espectrómetro inicializado")
    except Exception as e:
        logger.error(f"Error inicializando espectrómetro: {e}")

    yield

    try:
        state.spectrometer.disconnect()
    except Exception as e:
        logger.error(f"Error desconectando espectrómetro: {e}")


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
        current_mode=state.current_mode,
        spectrometer_connected=state.spectrometer.is_connected if state.spectrometer else False,
        last_error=state.last_error,
        spectrum_count=state.spectrum_count,
    )

@app.post("/api/config")
async def set_config(config: SystemConfig):
    if state.is_running:
        return {"status": "error", "message": "No se puede cambiar la configuración mientras corre la adquisición"}

    state.config = config
    logger.info(f"Configuración actualizada: modo={config.mode} ti={config.spec_params.ti_ms}ms")

    try:
        state.spectrometer.set_integration_time(config.spec_params.ti_ms)
    except Exception as e:
        logger.error(f"Error configurando espectrómetro: {e}")

    return {"status": "ok", "message": "Configuración aplicada"}

@app.post("/api/start")
async def start_acquisition():
    if state.is_running:
        return {"status": "error", "message": "Ya está corriendo"}
    if not state.config:
        return {"status": "error", "message": "No hay configuración cargada"}

    state.is_running     = True
    state.current_mode   = state.config.mode
    state.spectrum_count = 0
    state.last_error      = None
    state.spectra_buffer  = []
    state.acquisition_start_time = datetime.now()

    state.acquisition_task = asyncio.create_task(acquisition_loop())
    logger.info(f"Adquisición iniciada: {state.current_mode}")
    return {"status": "ok", "message": "Adquisición iniciada"}

@app.post("/api/stop")
async def stop_acquisition():
    if not state.is_running:
        return {"status": "error", "message": "No está corriendo"}
    state.is_running = False
    if state.acquisition_task and not state.acquisition_task.done():
        state.acquisition_task.cancel()
        try:
            await state.acquisition_task
        except (asyncio.CancelledError, Exception):
            pass
    logger.info("Adquisición detenida")
    return {"status": "ok", "message": "Adquisición detenida"}

@app.post("/api/save")
async def save_data():
    if not state.config:
        return {"status": "error", "message": "No hay configuración cargada"}
    if not state.spectra_buffer:
        return {"status": "error", "message": "No hay datos para guardar"}
    try:
        t = state.acquisition_start_time or datetime.now()
        result = await asyncio.to_thread(_write_file, list(state.spectra_buffer), t)
        state.spectra_buffer = []
        return result
    except Exception as e:
        logger.error(f"Error guardando: {e}")
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

    sample  = (meta.sample_name or 'muestra').strip().replace(' ', '_')
    day_dir = Path(meta.save_path) / day_str
    day_dir.mkdir(parents=True, exist_ok=True)
    out_file = day_dir / f"spec_{sample}_{date_compact}_{time_str}.txt"

    ti_list = [round(s['metadata'].get('ti_ms', 0), 1) for s in buf]
    ti_str  = str(ti_list[0]) if len(set(ti_list)) == 1 else str(ti_list)

    header = [
        "# Avantes Spectrometer Acquisition",
        f"# Muestra:        {meta.sample_name or 'N/A'}",
        f"# Fecha:          {day_str}",
        f"# Hora_inicio:    {start_time.strftime('%H:%M:%S')}",
        f"# Ti_ms:          {ti_str}",
        f"# Lambda_min_nm:  {round(wl[0], 2)}",
        f"# Lambda_max_nm:  {round(wl[-1], 2)}",
        f"# Promedios:      {cfg.spec_params.num_averages}",
        f"# Filtro_px:      {cfg.spec_params.filter_size}",
        f"# N_espectros:    {len(buf)}",
        "#",
    ]

    col_header = ["wl"] + [f"spec_{i+1}" for i in range(len(buf))]

    with open(out_file, 'w') as f:
        f.write('\n'.join(header) + '\n')
        f.write('\t'.join(col_header) + '\n')
        for i, wl_val in enumerate(wl):
            row = [f"{wl_val:.4f}"] + [f"{s['intensities'][i]:.2f}" for s in buf]
            f.write('\t'.join(row) + '\n')

    msg = f"Guardado: {day_str}/{out_file.name}  ({len(buf)} espectros)"
    logger.info(msg)
    return {"status": "ok", "message": msg, "path": str(out_file)}


@app.post("/api/spectrometer/auto_expose")
async def auto_expose():
    """Ajusta t_exp iterativamente para que el pico llegue al 75% de Q_MAX."""
    if state.is_running:
        return {"status": "error", "message": "Detener la adquisición antes de auto-exponer"}
    if not state.spectrometer or not state.spectrometer.is_connected:
        return {"status": "error", "message": "Espectrómetro no conectado"}

    TARGET       = 0.75 * Q_MAX
    T_MIN, T_MAX = 1.0, 25_000.0

    try:
        t_exp = float(state.spectrometer.measconfig.m_IntegrationTime)
        iters = []

        for it in range(6):
            await asyncio.to_thread(state.spectrometer.set_integration_time, t_exp)
            _, sp = await asyncio.to_thread(state.spectrometer.get_spectrum, 1, 1)
            peak  = float(max(sp))
            t_new = max(T_MIN, min(T_MAX, t_exp * TARGET / max(peak, 1.0)))

            entry = {"iter": it + 1, "t_ms": round(t_exp, 1),
                     "peak": int(peak), "t_new": round(t_new, 1),
                     "sat_pct": round(peak / Q_MAX * 100, 1)}
            iters.append(entry)
            logger.info(f"auto_expose iter {it+1}: t={t_exp:.0f}ms peak={int(peak)} "
                        f"sat={entry['sat_pct']}% → t_new={t_new:.0f}ms")

            if abs(t_new - t_exp) / max(t_exp, 1e-9) < 0.03:
                t_exp = t_new
                break
            t_exp = t_new

        await asyncio.to_thread(state.spectrometer.set_integration_time, t_exp)
        if state.config:
            state.config.spec_params.ti_ms = t_exp
        _, sp_final = await asyncio.to_thread(state.spectrometer.get_spectrum, 1, 1)
        final_peak = int(max(sp_final))
        final_sat  = round(final_peak / Q_MAX * 100, 1)

        return {
            "status":      "ok",
            "t_exp_ms":    round(t_exp, 1),
            "peak_counts": final_peak,
            "sat_pct":     final_sat,
            "iterations":  iters,
        }
    except Exception as e:
        logger.error(f"auto_expose error: {e}")
        return {"status": "error", "message": str(e)}


@app.post("/api/spectrometer/reconnect")
async def spectrometer_reconnect():
    if state.is_running:
        return {"status": "error", "message": "Detener la adquisición antes de reconectar"}
    try:
        await asyncio.to_thread(state.spectrometer.reconnect)
        connected = state.spectrometer.is_connected
        msg = "Espectrómetro reconectado" if connected else "No se detectó el espectrómetro (verificar USB)"
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
    logger.info(f"WebSocket conectado. Total: {len(state.active_websockets)}")

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
        logger.info("WebSocket desconectado")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        if websocket in state.active_websockets:
            state.active_websockets.remove(websocket)


# =============================================================================
# FUNCIONES DE ADQUISICIÓN
# =============================================================================

async def broadcast_spectrum(wavelengths: List[float], intensities: List[float], metadata: dict):
    entry = {'wavelengths': wavelengths, 'intensities': intensities, 'metadata': metadata}
    state.spectra_buffer.append(entry)

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
            logger.error(f"Error enviando a WebSocket: {e}")
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
    logger.info(f"Loop de adquisición iniciado: {config.mode}")

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
        logger.error(f"Error de adquisición: {e}")
        state.last_error = str(e)
    finally:
        state.is_running = False
        logger.info("Loop de adquisición finalizado")


# =============================================================================
# FRONTEND ESTÁTICO Y VISOR DE ARCHIVOS
# =============================================================================

BACKEND_DIR  = Path(__file__).parent
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"
DATA_DIR     = BACKEND_DIR.parent / "data"   # carpeta de datos por defecto (montar como volumen)

@app.get("/api/info")
async def get_info():
    return {"default_data_path": str(DATA_DIR)}

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

@app.get("/api/viewer/file")
async def viewer_file(folder: str, filename: str):
    path = DATA_DIR / Path(folder).name / Path(filename).name
    if not path.exists():
        return {"error": "Archivo no encontrado"}
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
        return {"error": "No se pudo leer el archivo"}
    arr = np.array(rows)
    spectra = {}
    for i, name in enumerate(col_names[1:], 1):
        if i >= arr.shape[1]:
            continue
        spectra[name] = arr[:, i].tolist()
    return {"meta": meta, "wavelengths": arr[:, 0].tolist(), "spectra": spectra}

if not FRONTEND_DIR.exists():
    logger.warning(f"No se encontró el directorio del frontend en {FRONTEND_DIR}")

    @app.get("/")
    async def root():
        return {"error": "Frontend not found", "expected_path": str(FRONTEND_DIR)}
else:
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
    logger.info(f"Sirviendo frontend desde {FRONTEND_DIR}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")

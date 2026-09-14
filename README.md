# UI Avantes — Backend + frontend de adquisición para espectrómetros Avantes

Aplicación dockerizada para controlar un espectrómetro Avantes (AvaSpec SDK /
`libavs.so`) y adquirir espectros desde el navegador. Sólo adquisición:
tiempo de integración, delay entre espectros, promediación y filtro de
suavizado. No controla motores ni shutter.

## 🏗️ Arquitectura

```
┌───────────────────────────────┐
│         Contenedor Docker      │
│         (Ubuntu 22.04)         │
│                                 │
│  FastAPI + libavs.so  ◄──USB──┼── Espectrómetro Avantes
│  Frontend estático (HTML/JS)   │
└──────────────┬──────────────────┘
               │ http://localhost:8000
         Navegador (cualquier SO)
```

El backend corre dentro del contenedor y controla el hardware por USB; el
frontend se sirve en el mismo puerto y se abre desde cualquier navegador que
llegue a esa IP/puerto — no requiere hotspot ni red dedicada.

## 📦 Requisitos

- Docker y Docker Compose.
- Espectrómetro Avantes conectado por USB al equipo que corre Docker.
- El instalador `vendor/avantes/libavs_9.14.0.0-0_amd64.deb` (incluido en el
  repo) — SDK propietario de Avantes, no redistribuir fuera de este uso.

### Acceso USB según sistema operativo

El contenedor necesita acceso directo al dispositivo USB del espectrómetro:

- **Linux (recomendado)**: Docker corre nativo sobre el kernel del host, el
  bus USB se pasa directo al contenedor. Es la misma base que ya funcionaba
  en la Raspberry Pi con Ubuntu, sólo que ahora corre en cualquier PC/mini PC
  con Linux + Docker.
- **Windows**: Docker Desktop corre sobre una VM (WSL2), así que el USB no es
  visible por defecto. Hace falta compartir el dispositivo con
  [`usbipd-win`](https://github.com/dorssel/usbipd-win) hacia WSL2 antes de
  levantar el contenedor.
- **macOS**: Docker Desktop también corre en una VM y no tiene un camino
  estable de passthrough USB. No está soportado de forma directa; se
  recomienda Linux o Windows+usbipd-win para uso real con el instrumento.

### Regla udev (host Linux)

Para que el usuario del host tenga permiso sobre el dispositivo USB antes de
pasarlo al contenedor:

```bash
sudo cp docker/99-avantes.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Desconectar y reconectar el espectrómetro después de instalar la regla.

## 🚀 Uso

### 1. Construir y levantar

```bash
docker compose up -d --build
```

Esto construye la imagen (Ubuntu 22.04 + SDK Avantes + backend + frontend) y
levanta el contenedor con:
- Puerto `8000` publicado en el host.
- `./data` (en el host) montado en `/data` (dentro del contenedor) — ahí se
  guardan los espectros.
- El bus USB del host mapeado al contenedor.

### 2. Abrir el frontend

```
http://localhost:8000
```

Desde otra PC en la misma red: `http://<IP-del-host>:8000`.

### 3. Flujo de trabajo

1. Configurar parámetros (Ti, modo, delay, promedios, filtro, muestra,
   carpeta de guardado) y presionar **SET**.
2. **START** inicia la adquisición (`SINGLE` = un espectro, `CONTINUOUS` =
   loop con el delay configurado).
3. El espectro se grafica en vivo vía WebSocket.
4. **STOP** detiene la adquisición.
5. **SAVE** guarda el buffer de espectros adquiridos en `/data/<fecha>/`.
6. **Ver espectros guardados** abre un visor para inspeccionar los archivos
   `.txt` guardados.

### Ver logs

```bash
docker compose logs -f
```

### Detener

```bash
docker compose down
```

## 🌐 API REST

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| GET | `/api/status` | Estado del sistema |
| POST | `/api/config` | Configurar parámetros de adquisición |
| POST | `/api/start` | Iniciar adquisición |
| POST | `/api/stop` | Detener adquisición |
| POST | `/api/save` | Guardar el buffer de espectros adquiridos |
| POST | `/api/spectrometer/auto_expose` | Ajustar automáticamente el tiempo de integración |
| POST | `/api/spectrometer/reconnect` | Reintentar conexión USB con el espectrómetro |
| GET | `/api/viewer/folders` | Carpetas de datos disponibles |
| GET | `/api/viewer/files` | Archivos guardados en una carpeta |
| GET | `/api/viewer/file` | Contenido de un archivo guardado |
| WS | `/ws/spectrum` | Streaming de espectros en vivo |

## 📊 Formato de datos guardados

Un archivo de texto por adquisición, en `/data/<YYYY-MM-DD>/spec_<muestra>_<fecha>_<hora>.txt`:

```
# Avantes Spectrometer Acquisition
# Muestra:        ejemplo
# Fecha:          2026-09-14
# Hora_inicio:    10:30:00
# Ti_ms:          100.0
# Lambda_min_nm:  285.12
# Lambda_max_nm:  539.87
# Promedios:      3
# Filtro_px:      1
# N_espectros:    5
#
wl      spec_1      spec_2      ...
285.12  1234.00     1245.00     ...
...
```

## 🛠️ Desarrollo local (sin Docker)

```bash
cd backend
pip3 install -r requirements.txt
python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Requiere `libavs.so` instalado en el sistema (ver `vendor/avantes/`) para
comunicarse con el hardware real; sin él, el backend corre en modo simulado
(espectros aleatorios) para poder probar la interfaz.

## 👨‍💻 Autor

Dr. Marcelo Raponi
División Sensado Remoto
DEILAP - CITEDEF, MINDEF

## 📄 Licencia

Uso académico/científico. El SDK de Avantes (`vendor/avantes/`) es software
propietario de Avantes BV, incluido únicamente para facilitar el build de
esta imagen.

<img src="docs/logo_DSR.jpeg" alt="División Sensado Remoto" width="220">

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

- **Linux (recomendado)**: Docker Engine nativo (`dockerd` como servicio del
  sistema, `apt install docker.io`/`docker-ce`) corre sobre el kernel del
  host y el bus USB se pasa directo al contenedor. Es la misma base que ya
  funcionaba en la Raspberry Pi con Ubuntu, sólo que ahora corre en cualquier
  PC/mini PC con Linux + Docker.

  ⚠️ **Docker Desktop en Linux NO sirve para esto**: aunque el SO sea Linux,
  Docker Desktop igual corre su motor dentro de una VM interna, así que
  `/dev/bus/usb` dentro del contenedor queda desconectado del USB real del
  host y el espectrómetro nunca aparece. Hay que usar el Docker Engine
  nativo (`docker context use default`, o directamente desinstalar Desktop y
  dejar sólo `docker.io`/`docker-ce` + el plugin `docker-compose-plugin`).
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

⚠️ **Si el espectrómetro se desconecta/reconecta o se corta la alimentación
mientras el contenedor está corriendo**, Linux le asigna un nuevo número de
dispositivo USB (ej: `Bus 004 Device 002` → `Bus 004 Device 009`). El
contenedor fija los nodos `/dev/bus/usb/...` al crearse, así que se queda con
el nodo viejo y el botón "Reconectar" del frontend no alcanza para verlo.
Solución: recrear el contenedor (no alcanza con un simple restart):

```bash
docker compose up -d --force-recreate
```

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

También se puede usar la imagen publicada en Docker Hub sin buildear
localmente — `docker-compose.yml` ya apunta a `mraponi74/ui-avantes:latest`,
así que `docker compose up -d` (sin `--build`) la descarga y levanta directo.

Si el puerto `8000` ya está en uso en el host, `docker compose up` va a
fallar ("port is already allocated"). Se puede cambiar sin editar el
archivo, con la variable `HOST_PORT`:

```bash
HOST_PORT=8080 docker compose up -d
```

### 2. Abrir el frontend

```
http://localhost:8000
```

Desde otra PC en la misma red: `http://<IP-del-host>:8000`.

### 3. Flujo de trabajo

1. Configurar parámetros (Ti, modo, delay, promedios, filtro, muestra,
   carpeta de guardado) — cada cambio se aplica solo, no hace falta un botón
   aparte.
2. **Iniciar** arranca la grabación (`SINGLE` = un espectro, `CONTINUOUS` =
   loop con el delay configurado); el contador "espectros acumulados"
   muestra en vivo cuántos hay pendientes de guardar.
3. El espectro se grafica en vivo vía WebSocket.
4. **Detener** frena la grabación.
5. **Guardar grabación** escribe el buffer de espectros acumulados en
   `/data/<fecha>/`.
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

## 🐳 Publicar la imagen en Docker Hub

```bash
docker login
docker build -t mraponi74/ui-avantes:latest .
docker push mraponi74/ui-avantes:latest
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

<img src="docs/logo_DSR.jpeg" alt="División Sensado Remoto" width="160">

**Dr. BioEng. Marcelo Raponi**

División Sensado Remoto (DSR), DEILAP
CITEDEF-UNIDEF (MINDEF - CONICET)

Juan Bautista de La Salle 4397
B1063ALO, Villa Martelli
Buenos Aires, Argentina

Tel: +54 11 4709 8100 ext. 1533
www.citedef.gob.ar

## 📄 Licencia

Uso académico/científico. El SDK de Avantes (`vendor/avantes/`) es software
propietario de Avantes BV, incluido únicamente para facilitar el build de
esta imagen.

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# Dependencias del sistema:
# - python3 / pip: backend FastAPI
# - libusb-1.0-0, libudev1: requeridos en runtime por libavs.so (SDK Avantes)
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        libusb-1.0-0 \
        libudev1 \
        udev \
    && rm -rf /var/lib/apt/lists/*

# --- SDK Avantes (AvaSpec / libavs) -----------------------------------------
# El paquete instala /lib64/libavs.so(.9.14.0.0). Ubuntu no busca en /lib64
# por defecto, así que se agrega al path de ldconfig.
COPY vendor/avantes/libavs_9.14.0.0-0_amd64.deb /tmp/libavs.deb
RUN dpkg -i /tmp/libavs.deb \
    && echo "/lib64" > /etc/ld.so.conf.d/avantes.conf \
    && ldconfig \
    && rm /tmp/libavs.deb
# ldconfig no cachea "libavs.so" (sin versión) para dlopen en runtime —
# se agrega /lib64 al LD_LIBRARY_PATH para que ctypes.CDLL("libavs.so") lo resuelva.
ENV LD_LIBRARY_PATH=/lib64

WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip3 install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

WORKDIR /app/backend
CMD ["python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# System dependencies:
# - python3 / pip: FastAPI backend
# - libusb-1.0-0, libudev1: required at runtime by libavs.so (Avantes SDK)
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        libusb-1.0-0 \
        libudev1 \
        udev \
    && rm -rf /var/lib/apt/lists/*

# --- Avantes SDK (AvaSpec / libavs) -----------------------------------------
# The package installs /lib64/libavs.so(.9.14.0.0). Ubuntu doesn't search
# /lib64 by default, so it's added to the ldconfig path.
COPY vendor/avantes/libavs_9.14.0.0-0_amd64.deb /tmp/libavs.deb
RUN dpkg -i /tmp/libavs.deb \
    && echo "/lib64" > /etc/ld.so.conf.d/avantes.conf \
    && ldconfig \
    && rm /tmp/libavs.deb
# ldconfig doesn't cache the unversioned "libavs.so" for runtime dlopen —
# /lib64 is added to LD_LIBRARY_PATH so ctypes.CDLL("libavs.so") can resolve it.
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

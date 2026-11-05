# NVIDIA variant: nvidia-smi comes from the NVIDIA Container Toolkit at run time
# (runtime: nvidia + NVIDIA_DRIVER_CAPABILITIES=utility), so a slim base is enough.
FROM python:3.11-slim

RUN pip install --no-cache-dir requests flask gunicorn

WORKDIR /app
COPY config.py ilo_fan.py loop.py app.py perf.py mclk.py /app/

# Settings persist here -- mount this as a volume so edits survive restarts.
RUN mkdir -p /data
VOLUME /data

EXPOSE 5000

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]

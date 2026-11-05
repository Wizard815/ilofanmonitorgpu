FROM rocm/rocm-terminal:latest

# rocm-terminal already ships rocm-smi. Add python deps for the Flask UI + loop.
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# --break-system-packages only exists on pip >=23 (PEP 668 guard) --
# this base image's pip3 is older and doesn't recognize it, so try with
# the flag first and fall back to a plain install if that flag errors.
RUN pip3 install --no-cache-dir --break-system-packages requests flask gunicorn \
    || pip3 install --no-cache-dir requests flask gunicorn

WORKDIR /app
COPY config.py ilo_fan.py loop.py app.py perf.py /app/

# Settings persist here -- mount this as a volume so edits survive restarts.
RUN mkdir -p /data
VOLUME /data

EXPOSE 5000

# gunicorn for a real WSGI server; --preload so the module-level
# `if __name__ == "__main__"` block (which starts the poll thread) doesn't
# run per-worker -- we start the thread explicitly below instead via a
# tiny wrapper, since gunicorn imports app.py without executing __main__.
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]

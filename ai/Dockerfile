ARG BASE_IMAGE=k1c-obico-cpu:df8190ef7500
FROM ${BASE_IMAGE}
COPY bridge.py /app/k1c_server.py
ENV PYTHONDONTWRITEBYTECODE=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
USER 10001:10001
CMD ["gunicorn", "--bind", "127.0.0.1:3333", "--workers", "1", "--threads", "2", "--timeout", "120", "--access-logfile", "/dev/null", "k1c_server:app"]

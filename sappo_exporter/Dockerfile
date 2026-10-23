FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sappo_exporter.py test_sappo_exporter.py ./
# Los tests se ejecutan al construir: si fallan, la imagen no se crea
RUN python -m unittest -q

USER nobody
EXPOSE 9105
HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import socket; socket.create_connection(('127.0.0.1', 9105), 3)"
CMD ["python", "sappo_exporter.py"]

FROM python:3.10-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

FROM base AS api-deps
COPY requirements/api.txt requirements/api.txt
RUN pip install --no-cache-dir -r requirements/api.txt

FROM api-deps AS api
COPY service/ service/
COPY migrations/ migrations/
CMD ["sh", "-c", "python -m service.datasets && exec gunicorn --bind=0.0.0.0:5000 --workers=2 --threads=4 --timeout=240 service.api:app"]

FROM api-deps AS worker-deps
ENV OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1 EASYOCR_MODULE_PATH=/models
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cpu
COPY requirements/worker.txt requirements/worker.txt
RUN pip install --no-cache-dir -r requirements/worker.txt

FROM worker-deps AS worker
COPY service/ service/
CMD ["python", "-m", "service.worker"]

FROM base AS ui
COPY requirements/ui.txt requirements/ui.txt
RUN pip install --no-cache-dir -r requirements/ui.txt
COPY ui/ /app/
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--browser.gatherUsageStats=false"]

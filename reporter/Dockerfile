FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/mpl TZ=Europe/Paris REPORT_DIR=/reports

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY report.py analyse.py template.html.j2 ./

RUN useradd -u 10001 -m reporter && mkdir -p /reports && chown reporter /reports
USER reporter

ENTRYPOINT ["python", "/app/report.py"]

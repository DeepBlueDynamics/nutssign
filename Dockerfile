# ============================================================
# nuts-sign — N.U.T.S. Signing Service — sign.nuts.services
# Build: docker build -t nuts-sign .
# Run:   docker run -p 8090:8080 -v nuts-sign-data:/data nuts-sign
# ============================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=8080
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY sign/ sign/
COPY templates/ templates/
COPY static/ static/

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8080

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips=*"]

FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/src
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock \
    && useradd --create-home simulator \
    && mkdir /data && chown simulator:simulator /data
COPY src ./src
USER simulator
ENV HELIO_DB_PATH=/data/helio.sqlite3
EXPOSE 8000
CMD ["uvicorn", "helio_simulator.api:app", "--host", "0.0.0.0", "--port", "8000"]

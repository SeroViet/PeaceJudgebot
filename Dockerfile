FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 FUSSBALL_STORAGE_DIR=/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m app && mkdir -p /data && chown app /data
USER app
EXPOSE 8000
CMD ["python", "main.py"]

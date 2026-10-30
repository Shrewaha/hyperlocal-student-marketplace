# Runs the backend anywhere that takes a container: Railway, Fly.io, Koyeb,
# a VPS, or your own machine.
#
#   docker build -t marketplace-api .
#   docker run -p 8000:8000 marketplace-api
#
# Render does not need this -- render.yaml covers it.

FROM python:3.12-slim

# Keeps .pyc files out of the image and makes log output appear immediately
# rather than sitting in a buffer, which matters when the logs are all you
# have to debug a hosted service.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EMAIL_CONSOLE_ONLY=true

WORKDIR /app

# Dependencies first, in their own layer: this step is only re-run when
# requirements.txt changes, so editing application code rebuilds in seconds.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/

WORKDIR /app/backend

# Most platforms inject $PORT; 8000 is the fallback for a plain docker run.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]

# --- Build/runtime image for the Price Service ----------------------------
FROM python:3.12-slim

WORKDIR /app

# Do not write .pyc files, do not buffer stdout - we want logs immediately.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./

# Create and switch to a non-root user.
RUN useradd --create-home --uid 10001 appuser
USER appuser

ENV PORT=8080
EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "warning"]

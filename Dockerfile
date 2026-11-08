FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OMP_THREAD_LIMIT=1 \
    INGESTION_TEMP_DIR=/tmp/eduforge

# OCR + legacy .doc support. Arabic AND English language data are required for ingestion of Arabic documents.
# Set INSTALL_LIBREOFFICE=true for best-fidelity .doc conversion (adds several hundred MB); antiword is the default.
ARG INSTALL_LIBREOFFICE=false
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-ara tesseract-ocr-eng antiword fonts-dejavu-core \
    && if [ "$INSTALL_LIBREOFFICE" = "true" ]; then \
         apt-get install -y --no-install-recommends libreoffice-writer; \
       fi \
    && rm -rf /var/lib/apt/lists/* \
    && tesseract --list-langs | grep -q "^ara$" \
    && tesseract --list-langs | grep -q "^eng$"

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /tmp/eduforge /srv/storage/documents \
    && chown -R appuser:appuser /tmp/eduforge /srv/storage
COPY --chown=appuser:appuser . .
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

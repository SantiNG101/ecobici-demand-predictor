FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements-despliegue.txt .
RUN pip install --no-cache-dir -r requirements-despliegue.txt

COPY src/ src/
COPY api/ api/
COPY interfaz/ interfaz/

# models/ y data/processed/ se montan como volúmenes (el dataset pesa 434 MB)
EXPOSE 8000 8501
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]

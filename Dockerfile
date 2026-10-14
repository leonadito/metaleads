# ---- Stage 1: Tailwind CSS of the CRM screens (same pattern as the core) ----
FROM node:24-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
COPY templates/ /app/templates/
COPY crm/ /app/crm/
RUN npm run build:css

# ---- Stage 2: the Django app ----
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=frontend-build /app/static/css/tailwind.css ./static/css/tailwind.css

EXPOSE 8000

CMD ["gunicorn", "leadmanager.wsgi:application"]

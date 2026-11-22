FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt
COPY backend/ /app/backend/
COPY frontend/CHEETAH_UI_Prototype_v1.5.html /app/frontend/CHEETAH_UI_Prototype_v1.5.html
WORKDIR /app/backend
EXPOSE 8000

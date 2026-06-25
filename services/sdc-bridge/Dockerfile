FROM python:3.12-slim
RUN pip install --no-cache-dir sdc11073==2.4.1 fastapi==0.115.6 "uvicorn[standard]==0.34.0"
WORKDIR /app
COPY app.py .
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

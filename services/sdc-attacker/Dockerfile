FROM python:3.12-slim
RUN pip install --no-cache-dir sdc11073==2.4.1 fastapi==0.115.6 "uvicorn[standard]==0.34.0"
WORKDIR /app
COPY app.py rogue_monitor.py rogue_ventilator.py monitor_mdib.xml ventilator_mdib.xml /app/
COPY static /app/static
EXPOSE 8090
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090"]

FROM python:3.12-slim
RUN pip install --no-cache-dir sdc11073==2.4.1
WORKDIR /app
COPY mdib.xml provider.py /app/
CMD ["python", "provider.py"]

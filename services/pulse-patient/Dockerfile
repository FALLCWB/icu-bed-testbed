# pulse-patient — validated physiology backend built on the Pulse Physiology
# Engine (Kitware, Apache-2.0).
#
# De-risk result (2026-06-23): the Pulse Python API has no PyPI wheel and must
# otherwise be built from C++ source. Kitware, however, publishes a prebuilt
# image (kitware/pulse) that ships the compiled PyPulse extension and the python
# wrapper package, with PYTHONPATH already set to /pulse/bin:/pulse/python. We
# base on it directly — no source build. The pulse python package imports pandas
# and numpy at load time (per the official Binder requirements), so those are
# installed here along with the web stack. Patient state files live under
# /pulse/bin (relative ./states/... paths), so the workdir must be /pulse/bin.
FROM kitware/pulse:4.3.1

RUN pip3 install --no-cache-dir \
    numpy pandas \
    fastapi==0.115.6 "uvicorn[standard]==0.34.0" pydantic==2.10.4

COPY app.py /pulse/bin/app.py
WORKDIR /pulse/bin
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

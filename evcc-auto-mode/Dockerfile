ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app

COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app

CMD ["python3", "/app/main.py"]

FROM python:3.12-alpine
ARG VERSION=v1
ENV VERSION=$VERSION PYTHONUNBUFFERED=1
COPY app.py /app.py
RUN mkdir -p /state /cache && chown 65534:65534 /state /cache
USER 65534
EXPOSE 8080
CMD ["python", "/app.py"]

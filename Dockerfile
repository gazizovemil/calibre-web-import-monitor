FROM python:3.13-alpine

RUN apk add --no-cache su-exec

WORKDIR /app

COPY app.py /app/app.py
COPY index.html /app/index.html
COPY favicon.svg /app/favicon.svg
COPY lang /app/lang
COPY docker-entrypoint.sh /docker-entrypoint.sh

RUN chmod 755 /docker-entrypoint.sh

EXPOSE 8087

ENTRYPOINT ["/docker-entrypoint.sh"]

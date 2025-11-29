FROM ubuntu:20.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y python3-pip vim net-tools curl && \
    pip3 install flask requests && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

CMD ["python3", "/app/app.py"]

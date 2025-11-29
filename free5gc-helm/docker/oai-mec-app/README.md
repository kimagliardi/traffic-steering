# OAI MEC App Docker Image

Pre-built image with Flask and dependencies to avoid runtime internet dependency.

## Build

```bash
docker build -t localhost:32000/mec-app:v1 .
docker push localhost:32000/mec-app:v1
```

## Why this exists

The original deployment ran `apt update && apt install` on every container startup, which required internet access. When UPFs are running, the container's traffic gets routed through the 5G network and can't reach the internet for package installation.

This pre-built image has all dependencies installed at build time, so the container can start without internet access.

## Dependencies included

- python3-pip
- vim
- net-tools
- curl
- Flask
- requests

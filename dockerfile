FROM python:3.13-slim

RUN apt-get update && apt-get install -y patchelf libglib2.0-0 build-essential && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y patchelf libglib2.0-0 && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash nuitka
WORKDIR /home/nuitka/
RUN chown -R nuitka:nuitka /home/nuitka
USER nuitka
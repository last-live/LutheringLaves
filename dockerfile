FROM python:3.13-slim

RUN apt-get update && apt-get install -y patchelf libglib2.0-0 build-essential zip && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /home/nuitka/
RUN pip install -r /home/nuitka/requirements.txt && rm /home/nuitka/requirements.txt

RUN useradd -m -s /bin/bash nuitka
WORKDIR /home/nuitka/
RUN chown -R nuitka:nuitka /home/nuitka
USER nuitka
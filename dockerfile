FROM moonbuggy2000/nuitka:2.7.6-py3.13-debian

RUN apt-get update && apt-get install -y patchelf && rm -rf /var/lib/apt/lists/*

ENTRYPOINT ["tail", "-f", "/dev/null"]
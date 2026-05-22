FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y \
    build-essential \
    libopus-dev \
    libvpx-dev \
    libffi-dev \
    libssl-dev \
    ffmpeg \
    pkg-config \
    libavdevice-dev \
    libavfilter-dev \
    libavformat-dev \
    libavcodec-dev \
    libswresample-dev \
    libswscale-dev \
    libavutil-dev \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir openai>=1.0.0

RUN python3 -c "from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport;from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection;print('All imports OK')"

RUN python3 -c "from pipecat.audio.vad.silero import SileroVADAnalyzer; SileroVADAnalyzer()"

COPY bot/ ./bot/
COPY server.py .
COPY main.py .
EXPOSE 7860

CMD ["python3", "server.py"]
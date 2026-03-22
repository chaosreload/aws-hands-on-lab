#!/usr/bin/env python3
"""
Python WebRTC client for AgentCore voice agent end-to-end testing.
v4: Fixed stereo→mono: aiortc Opus packed s16 interleave must reshape before channel merge.

Replaces browser: sends TTS audio via WebRTC, records agent response.
Uses AgentCore Runtime API for signaling (ICE config + SDP exchange).
"""
import asyncio
import base64
import fractions
import json
import struct
import subprocess
import sys
import time
import uuid

import boto3
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.mediastreams import AudioFrame, MediaStreamTrack
import numpy as np

# === Configuration ===
AGENT_ARN = "arn:aws:bedrock-agentcore:us-east-1:595842667825:runtime/webrtc_audio_fix-H8l8sS76kI"
REGION = "us-east-1"
PROFILE = "weichaol-testenv2-awswhatsnewtest"
INPUT_WAV = "/tmp/question_16k.wav"
OUTPUT_WAV = "/tmp/response.wav"
QUESTION_COPY = "/tmp/question_sent.wav"
RAW_OUTPUT_WAV = "/tmp/response_raw.wav"

# Audio constants
INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000  # Nova Sonic output rate
FRAME_DURATION_MS = 20
INPUT_SAMPLES_PER_FRAME = INPUT_SAMPLE_RATE * FRAME_DURATION_MS // 1000  # 320
BYTES_PER_SAMPLE = 2

# Timing
MAX_WAIT_RESPONSE_S = 30
SILENCE_AFTER_AUDIO_S = 2
POST_RESPONSE_SILENCE_S = 3


class FileAudioTrack(MediaStreamTrack):
    """WebRTC audio track that streams PCM from a WAV file, then silence."""

    kind = "audio"

    def __init__(self, wav_path):
        super().__init__()
        self._pcm_data = self._load_wav(wav_path)
        self._offset = 0
        self._frame_count = 0
        self._start_time = None
        self._finished = False

    @staticmethod
    def _load_wav(path):
        with open(path, 'rb') as f:
            f.read(44)
            return f.read()

    async def recv(self):
        if self._start_time is None:
            self._start_time = time.time()

        target = self._start_time + self._frame_count * (FRAME_DURATION_MS / 1000)
        delay = target - time.time()
        if delay > 0:
            await asyncio.sleep(delay)

        frame_bytes = INPUT_SAMPLES_PER_FRAME * BYTES_PER_SAMPLE

        if self._offset < len(self._pcm_data):
            chunk = self._pcm_data[self._offset:self._offset + frame_bytes]
            self._offset += frame_bytes
            if len(chunk) < frame_bytes:
                chunk += b'\x00' * (frame_bytes - len(chunk))
        else:
            chunk = b'\x00' * frame_bytes
            if not self._finished:
                self._finished = True
                print(f"[TX] Audio playback complete at frame {self._frame_count}")

        frame = AudioFrame(format='s16', layout='mono', samples=INPUT_SAMPLES_PER_FRAME)
        frame.planes[0].update(chunk)
        frame.sample_rate = INPUT_SAMPLE_RATE
        frame.pts = self._frame_count * INPUT_SAMPLES_PER_FRAME
        frame.time_base = fractions.Fraction(1, INPUT_SAMPLE_RATE)
        self._frame_count += 1
        return frame

    @property
    def audio_finished(self):
        return self._finished


class AudioRecorder:
    """Records incoming WebRTC audio frames - properly handles stereo Opus decode."""

    def __init__(self):
        self._chunks = []  # list of numpy arrays (mono int16)
        self._first_audio_time = None
        self._last_audio_time = None
        self._frame_count = 0
        self._native_sample_rate = None
        self._logged_first = False

    def add_frame(self, frame):
        """Add an audio frame, convert to mono numpy array."""
        self._native_sample_rate = frame.sample_rate

        # Log first frame properties
        if not self._logged_first:
            print(f"[RX] First frame: rate={frame.sample_rate}, format={frame.format.name}, "
                  f"layout={frame.layout.name}, samples={frame.samples}, "
                  f"planes={len(frame.planes)}, plane_sizes={[len(bytes(p)) for p in frame.planes]}")

        # Use to_ndarray() for proper format handling
        try:
            arr = frame.to_ndarray()
            # arr shape depends on format:
            # - packed (s16): shape = (1, samples * channels) or (samples, channels)
            # - planar (s16p): shape = (channels, samples)

            if not self._logged_first:
                print(f"[RX] ndarray: shape={arr.shape}, dtype={arr.dtype}, "
                      f"min={arr.min()}, max={arr.max()}")
                self._logged_first = True

            # Convert to mono int16
            if arr.dtype != np.int16:
                # Float format - convert to int16
                arr = (arr * 32767).clip(-32768, 32767).astype(np.int16)

            # Flatten to 1D first
            flat = arr.flatten().astype(np.int16)

            # Detect stereo: aiortc Opus decoder outputs packed s16 stereo
            # with L/R interleaved [L0,R0,L1,R1,...]. frame.layout.channels
            # tells us the real channel count.
            n_channels = len(frame.layout.channels)
            if n_channels > 1 and len(flat) % n_channels == 0:
                # Reshape to (samples, channels) and average → true mono
                reshaped = flat.reshape(-1, n_channels)
                mono = reshaped.mean(axis=1).astype(np.int16)
            else:
                mono = flat

            max_amp = int(np.max(np.abs(mono)))

            if self._frame_count < 5 or self._frame_count % 200 == 0:
                print(f"[RX] Frame {self._frame_count}: mono_samples={len(mono)}, "
                      f"max={max_amp}, mean={np.mean(np.abs(mono)):.0f}")

            if max_amp > 100:
                if self._first_audio_time is None:
                    self._first_audio_time = time.time()
                self._last_audio_time = time.time()

            self._chunks.append(mono)
            self._frame_count += 1

        except Exception as e:
            if not self._logged_first:
                self._logged_first = True
            # Fallback: raw bytes
            pcm = bytes(frame.planes[0])
            print(f"[RX] to_ndarray failed ({e}), raw fallback: {len(pcm)} bytes")
            arr = np.frombuffer(pcm, dtype=np.int16)
            self._chunks.append(arr)
            self._frame_count += 1

    def save_wav(self, raw_path, output_path, target_rate=24000):
        """Save recorded audio: raw WAV at native rate, then ffmpeg convert."""
        if not self._chunks:
            print("[RX] No audio recorded!")
            return

        # Concatenate all mono chunks
        mono_data = np.concatenate(self._chunks)
        pcm_data = mono_data.tobytes()
        native_rate = self._native_sample_rate or 48000

        # Save raw WAV at native sample rate (properly mono)
        self._write_wav(raw_path, pcm_data, native_rate)
        raw_duration = len(mono_data) / native_rate
        print(f"[RX] Saved raw: {raw_path} ({raw_duration:.2f}s, {native_rate}Hz, "
              f"{len(pcm_data)} bytes, {self._frame_count} frames, "
              f"total_samples={len(mono_data)})")

        # Use ffmpeg for reliable conversion to target rate
        if native_rate != target_rate:
            cmd = [
                'ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning',
                '-i', raw_path,
                '-ar', str(target_rate),
                '-ac', '1',
                '-acodec', 'pcm_s16le',
                output_path
            ]
            print(f"[RX] Converting {native_rate}Hz → {target_rate}Hz via ffmpeg...")
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                print(f"[RX] ffmpeg error: {result.stderr}")
                import shutil
                shutil.copy2(raw_path, output_path)
            else:
                print(f"[RX] Saved converted: {output_path}")
        else:
            import shutil
            shutil.copy2(raw_path, output_path)
            print(f"[RX] Native rate matches target, copied directly")

        # Verify with ffprobe
        try:
            probe = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries',
                 'stream=sample_rate,channels,duration,codec_name',
                 '-of', 'json', output_path],
                capture_output=True, text=True
            )
            if probe.returncode == 0:
                info = json.loads(probe.stdout)
                stream = info.get('streams', [{}])[0]
                print(f"[RX] Verified: codec={stream.get('codec_name')}, "
                      f"rate={stream.get('sample_rate')}, "
                      f"channels={stream.get('channels')}, "
                      f"duration={stream.get('duration')}s")
        except Exception as e:
            print(f"[RX] ffprobe check skipped: {e}")

        # Audio quality stats
        try:
            stats = subprocess.run(
                ['ffmpeg', '-i', output_path, '-af',
                 'volumedetect', '-f', 'null', '-'],
                capture_output=True, text=True
            )
            for line in stats.stderr.split('\n'):
                if 'mean_volume' in line or 'max_volume' in line:
                    print(f"[RX] {line.strip()}")
        except:
            pass

    @staticmethod
    def _write_wav(path, pcm_data, sample_rate):
        """Write raw mono PCM data as a WAV file."""
        data_size = len(pcm_data)
        with open(path, 'wb') as f:
            f.write(b'RIFF')
            f.write(struct.pack('<I', 36 + data_size))
            f.write(b'WAVE')
            f.write(b'fmt ')
            f.write(struct.pack('<I', 16))
            f.write(struct.pack('<H', 1))    # PCM
            f.write(struct.pack('<H', 1))    # mono
            f.write(struct.pack('<I', sample_rate))
            f.write(struct.pack('<I', sample_rate * 2))
            f.write(struct.pack('<H', 2))    # block align
            f.write(struct.pack('<H', 16))   # bits per sample
            f.write(b'data')
            f.write(struct.pack('<I', data_size))
            f.write(pcm_data)

    @property
    def has_audio(self):
        return self._first_audio_time is not None

    @property
    def seconds_since_last_audio(self):
        if self._last_audio_time is None:
            return float('inf')
        return time.time() - self._last_audio_time


def invoke_agent(session, agent_arn, session_id, payload):
    """Invoke AgentCore Runtime API."""
    client = session.client('bedrock-agentcore', region_name=REGION)
    t0 = time.time()
    response = client.invoke_agent_runtime(
        agentRuntimeArn=agent_arn,
        runtimeSessionId=session_id,
        contentType='application/json',
        accept='application/json',
        payload=json.dumps(payload).encode('utf-8')
    )
    elapsed = time.time() - t0
    body = response['response'].read()
    return json.loads(body), elapsed


async def main():
    t_start = time.time()
    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    session_id = str(uuid.uuid4())
    print(f"Session ID: {session_id}")

    # === Step 1: Get ICE config ===
    print("\n[1] Getting ICE config...")
    ice_response, ice_latency = invoke_agent(
        session, AGENT_ARN, session_id, {"action": "ice_config"}
    )
    print(f"    ICE config latency: {ice_latency:.2f}s")
    print(f"    TURN servers: {len(ice_response.get('iceServers', []))}")

    ice_servers = []
    for s in ice_response.get('iceServers', []):
        urls = s.get('urls', [])
        turn_urls = [u for u in urls if u.startswith('turn:')]
        if turn_urls:
            ice_servers.append(RTCIceServer(
                urls=turn_urls,
                username=s.get('username'),
                credential=s.get('credential')
            ))
    print(f"    Using {len(ice_servers)} TURN servers")

    # === Step 2: Create peer connection ===
    print("\n[2] Creating WebRTC peer connection...")
    config = RTCConfiguration(iceServers=ice_servers)
    pc = RTCPeerConnection(config)
    recorder = AudioRecorder()

    @pc.on("track")
    def on_track(track):
        print(f"    Received remote track: {track.kind}")
        if track.kind == "audio":
            asyncio.ensure_future(_record_track(track, recorder))

    @pc.on("iceconnectionstatechange")
    async def on_ice_state():
        print(f"    ICE state: {pc.iceConnectionState}")

    @pc.on("connectionstatechange")
    async def on_conn_state():
        print(f"    Connection state: {pc.connectionState}")

    audio_track = FileAudioTrack(INPUT_WAV)
    pc.addTrack(audio_track)

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    print(f"    Local SDP created ({len(offer.sdp)} bytes)")

    # === Step 3: Exchange SDP ===
    print("\n[3] Exchanging SDP offer/answer...")
    offer_payload = {
        "action": "offer",
        "data": {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
            "turnOnly": True
        }
    }
    answer_response, sdp_latency = invoke_agent(
        session, AGENT_ARN, session_id, offer_payload
    )
    print(f"    SDP exchange latency: {sdp_latency:.2f}s")

    pc_id = answer_response.get('pc_id')
    print(f"    Remote pc_id: {pc_id}")

    answer = RTCSessionDescription(
        sdp=answer_response['sdp'],
        type=answer_response['type']
    )
    await pc.setRemoteDescription(answer)
    print(f"    Remote SDP set ({len(answer_response['sdp'])} bytes)")

    # === Step 4: Wait for connection ===
    print("\n[4] Waiting for WebRTC connection...")
    t_connect_start = time.time()

    for i in range(60):
        await asyncio.sleep(0.5)
        state = pc.connectionState
        if state == "connected":
            t_connected = time.time()
            print(f"    Connected! ({t_connected - t_connect_start:.2f}s)")
            break
        elif state == "failed":
            print(f"    Connection FAILED!")
            await pc.close()
            return
    else:
        print(f"    Connection timeout (30s)")
        await pc.close()
        return

    # === Step 5: Stream audio ===
    print("\n[5] Streaming audio to agent...")
    audio_duration = len(audio_track._pcm_data) / (INPUT_SAMPLE_RATE * BYTES_PER_SAMPLE)
    print(f"    Audio duration: {audio_duration:.2f}s")

    while not audio_track.audio_finished:
        await asyncio.sleep(0.1)

    print(f"    Audio sent. Waiting for agent response...")

    t_wait_start = time.time()
    while time.time() - t_wait_start < MAX_WAIT_RESPONSE_S:
        await asyncio.sleep(0.5)
        if recorder.has_audio:
            if recorder.seconds_since_last_audio > POST_RESPONSE_SILENCE_S:
                print(f"    Response complete (silence detected)")
                break
    else:
        if recorder.has_audio:
            print(f"    Timeout but got some audio")
        else:
            print(f"    No response audio received within {MAX_WAIT_RESPONSE_S}s")

    # === Step 6: Save results ===
    print("\n[6] Saving audio files...")
    import shutil
    shutil.copy2(INPUT_WAV, QUESTION_COPY)
    print(f"[TX] Saved {QUESTION_COPY}")

    recorder.save_wav(RAW_OUTPUT_WAV, OUTPUT_WAV, target_rate=OUTPUT_SAMPLE_RATE)

    # === Step 7: Disconnect ===
    print("\n[7] Disconnecting...")
    try:
        disconnect_payload = {"action": "disconnect", "data": {"pc_id": pc_id}}
        invoke_agent(session, AGENT_ARN, session_id, disconnect_payload)
    except Exception as e:
        print(f"    Disconnect error (non-fatal): {e}")
    await pc.close()

    # === Summary ===
    t_end = time.time()
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total time:           {t_end - t_start:.2f}s")
    print(f"ICE config latency:   {ice_latency:.2f}s")
    print(f"SDP exchange latency: {sdp_latency:.2f}s")
    print(f"Connection time:      {t_connected - t_connect_start:.2f}s")
    print(f"Audio sent:           {audio_duration:.2f}s")
    if recorder._first_audio_time:
        first_audio_delay = recorder._first_audio_time - t_connected
        response_duration = recorder._last_audio_time - recorder._first_audio_time
        print(f"First response audio: {first_audio_delay:.2f}s after connect")
        print(f"Response duration:    {response_duration:.2f}s")
    else:
        print(f"First response audio: NONE")
    print(f"Response frames:      {recorder._frame_count}")
    print(f"Native sample rate:   {recorder._native_sample_rate}Hz")
    print(f"Question file:        {QUESTION_COPY}")
    print(f"Response file (raw):  {RAW_OUTPUT_WAV}")
    print(f"Response file:        {OUTPUT_WAV}")


async def _record_track(track, recorder):
    """Record audio from a remote WebRTC track."""
    try:
        while True:
            frame = await track.recv()
            recorder.add_frame(frame)
    except Exception as e:
        if "MediaStreamError" not in str(type(e)):
            print(f"[RX] Recording stopped: {e}")


if __name__ == "__main__":
    asyncio.run(main())

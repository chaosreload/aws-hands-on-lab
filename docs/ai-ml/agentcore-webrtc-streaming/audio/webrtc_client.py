#!/usr/bin/env python3
"""
Python WebRTC client for AgentCore voice agent end-to-end testing.

Replaces browser: sends TTS audio via WebRTC, records agent response.
Uses AgentCore Runtime API for signaling (ICE config + SDP exchange).
"""
import asyncio
import base64
import fractions
import json
import struct
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
import av

# === Configuration ===
AGENT_ARN = "arn:aws:bedrock-agentcore:us-east-1:595842667825:runtime/bot-6KP2KQ5hWC"
REGION = "us-east-1"
PROFILE = "weichaol-testenv2-awswhatsnewtest"
INPUT_WAV = "/tmp/question_16k.wav"
OUTPUT_WAV = "/tmp/response.wav"
QUESTION_COPY = "/tmp/question_sent.wav"  # copy of what we actually sent

# Audio constants
INPUT_SAMPLE_RATE = 16000  # What we send (matches Nova Sonic input)
OUTPUT_SAMPLE_RATE = 24000  # What Nova Sonic produces
FRAME_DURATION_MS = 20
INPUT_SAMPLES_PER_FRAME = INPUT_SAMPLE_RATE * FRAME_DURATION_MS // 1000  # 320
BYTES_PER_SAMPLE = 2

# Timing
MAX_WAIT_RESPONSE_S = 30  # Max seconds to wait for agent response
SILENCE_AFTER_AUDIO_S = 2  # Silence after sending audio before we stop
POST_RESPONSE_SILENCE_S = 3  # After response starts, wait this long after last audio


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
        """Load 16kHz 16-bit mono PCM from WAV file."""
        with open(path, 'rb') as f:
            # Skip WAV header (44 bytes)
            f.read(44)
            return f.read()

    async def recv(self):
        if self._start_time is None:
            self._start_time = time.time()

        # Pace to real-time
        target = self._start_time + self._frame_count * (FRAME_DURATION_MS / 1000)
        delay = target - time.time()
        if delay > 0:
            await asyncio.sleep(delay)

        frame_bytes = INPUT_SAMPLES_PER_FRAME * BYTES_PER_SAMPLE  # 640 bytes per 20ms

        if self._offset < len(self._pcm_data):
            chunk = self._pcm_data[self._offset:self._offset + frame_bytes]
            self._offset += frame_bytes
            # Pad if last chunk is short
            if len(chunk) < frame_bytes:
                chunk += b'\x00' * (frame_bytes - len(chunk))
        else:
            # Send silence after audio finishes
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
    """Records incoming WebRTC audio frames to a buffer."""

    def __init__(self):
        self._chunks = []
        self._first_audio_time = None
        self._last_audio_time = None
        self._frame_count = 0
        self._resampler = av.AudioResampler(format='s16', layout='mono', rate=OUTPUT_SAMPLE_RATE)

    def add_frame(self, frame):
        """Add an audio frame, skip silence."""
        # Convert to expected format
        resampled = self._resampler.resample(frame)
        for rf in resampled:
            pcm = bytes(rf.planes[0])
            # Check if frame has audio (not just silence)
            samples = struct.unpack(f'<{len(pcm)//2}h', pcm)
            max_amp = max(abs(s) for s in samples) if samples else 0

            if max_amp > 100:  # threshold for non-silence
                if self._first_audio_time is None:
                    self._first_audio_time = time.time()
                self._last_audio_time = time.time()

            self._chunks.append(pcm)
            self._frame_count += 1

    def save_wav(self, path):
        """Save recorded audio as WAV file."""
        if not self._chunks:
            print("[RX] No audio recorded!")
            return

        pcm_data = b''.join(self._chunks)
        sample_rate = OUTPUT_SAMPLE_RATE
        data_size = len(pcm_data)

        with open(path, 'wb') as f:
            f.write(b'RIFF')
            f.write(struct.pack('<I', 36 + data_size))
            f.write(b'WAVE')
            f.write(b'fmt ')
            f.write(struct.pack('<I', 16))
            f.write(struct.pack('<H', 1))   # PCM
            f.write(struct.pack('<H', 1))   # mono
            f.write(struct.pack('<I', sample_rate))
            f.write(struct.pack('<I', sample_rate * 2))
            f.write(struct.pack('<H', 2))   # block align
            f.write(struct.pack('<H', 16))  # bits
            f.write(b'data')
            f.write(struct.pack('<I', data_size))
            f.write(pcm_data)

        duration = data_size / (sample_rate * 2)
        print(f"[RX] Saved {path}: {duration:.2f}s, {data_size} bytes, {self._frame_count} frames")

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
        # Use only TURN (not STUN) for relay
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

    # Track incoming audio
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

    # Add our audio track
    audio_track = FileAudioTrack(INPUT_WAV)
    pc.addTrack(audio_track)

    # Create offer
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    print(f"    Local SDP created ({len(offer.sdp)} bytes)")

    # === Step 3: Exchange SDP via AgentCore ===
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

    # Set remote description
    answer = RTCSessionDescription(
        sdp=answer_response['sdp'],
        type=answer_response['type']
    )
    await pc.setRemoteDescription(answer)
    print(f"    Remote SDP set ({len(answer_response['sdp'])} bytes)")

    # === Step 4: Wait for connection + stream audio ===
    print("\n[4] Waiting for WebRTC connection...")
    t_connect_start = time.time()

    # Wait for connection
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

    # Audio is already streaming via FileAudioTrack
    print("\n[5] Streaming audio to agent...")
    audio_duration = len(audio_track._pcm_data) / (INPUT_SAMPLE_RATE * BYTES_PER_SAMPLE)
    print(f"    Audio duration: {audio_duration:.2f}s")

    # Wait for audio to finish sending + some silence
    while not audio_track.audio_finished:
        await asyncio.sleep(0.1)

    print(f"    Audio sent. Waiting for agent response...")

    # Wait for response with timeout
    t_wait_start = time.time()
    while time.time() - t_wait_start < MAX_WAIT_RESPONSE_S:
        await asyncio.sleep(0.5)
        if recorder.has_audio:
            # Got response audio, wait for it to finish
            if recorder.seconds_since_last_audio > POST_RESPONSE_SILENCE_S:
                print(f"    Response complete (silence detected)")
                break
    else:
        if recorder.has_audio:
            print(f"    Timeout but got some audio")
        else:
            print(f"    No response audio received within {MAX_WAIT_RESPONSE_S}s")

    # === Step 5: Save results ===
    print("\n[6] Saving audio files...")
    # Save the question audio (copy)
    import shutil
    shutil.copy2(INPUT_WAV, QUESTION_COPY)
    print(f"[TX] Saved {QUESTION_COPY}")

    # Save response
    recorder.save_wav(OUTPUT_WAV)

    # === Step 6: Disconnect ===
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
        print(f"First response audio: {first_audio_delay:.2f}s after connect")
    else:
        print(f"First response audio: NONE")
    print(f"Response frames:      {recorder._frame_count}")
    print(f"Question file:        {QUESTION_COPY}")
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

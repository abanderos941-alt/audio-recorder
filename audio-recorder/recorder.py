"""
Audio recorder: microphone + system speakers (WASAPI loopback) -> MP3
Windows 11, pyaudiowpatch + lameenc

Auto-split on silence: when audio drops below threshold for silence_duration
seconds, the current file is closed and a new one starts on next speech.

Usage:
    python recorder.py              # record until Enter
    python recorder.py 30           # record for 30 seconds
"""
from __future__ import annotations

import sys
import threading
import queue
import time
import wave as _wave
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np
import pyaudiowpatch as pyaudio
import lameenc

# ── Defaults ──────────────────────────────────────────────────────────────────
SAMPLE_RATE      = 48000
CHANNELS         = 2
CHUNK            = 1024
FORMAT           = pyaudio.paInt16
MP3_BITRATE      = 128
OUTPUT_DIR       = Path(__file__).parent / "recordings"
SILENCE_RMS      = 500
SILENCE_DURATION = 0.9
MIN_SPEECH_DUR   = 0.5
PRE_ROLL_SECS    = 0.25   # frames kept before detected speech onset
POST_ROLL_SECS   = 0.35   # frames kept after last speech (protects word endings)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_stereo(data: bytes, src_ch: int) -> np.ndarray:
    arr = np.frombuffer(data, dtype=np.int16)
    if src_ch == 1:
        return np.column_stack([arr, arr]).flatten()
    if src_ch > 2:
        return arr.reshape(-1, src_ch)[:, :2].flatten()
    return arr


def _resample(arr: np.ndarray, src_rate: int) -> np.ndarray:
    if src_rate == SAMPLE_RATE:
        return arr
    n_out = max(2, int(len(arr) * SAMPLE_RATE / src_rate)) & ~1  # even — stereo frame alignment
    return np.interp(np.linspace(0, len(arr) - 1, n_out),
                     np.arange(len(arr)),
                     arr.astype(np.float32)).astype(np.int16)


def _mix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    n = min(len(a), len(b)) & ~1  # even — stereo frame alignment
    return np.clip(a[:n].astype(np.int32) + b[:n].astype(np.int32), -32768, 32767).astype(np.int16)


def _rms(arr: np.ndarray) -> float:
    return float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))


# ── Recorder ──────────────────────────────────────────────────────────────────

class Recorder:
    def __init__(self,
                 silence_rms: int = SILENCE_RMS,
                 silence_duration: float = SILENCE_DURATION,
                 min_speech_duration: float = MIN_SPEECH_DUR,
                 mp3_bitrate: int = MP3_BITRATE,
                 output_dir: str | Path = OUTPUT_DIR,
                 min_record_secs: float = 0.0,
                 idle_timeout_secs: float = 0.0,
                 output_format: str = 'mp3',
                 full_record: bool = False,
                 full_output_dir: str | Path = '',
                 mic_device: int | None = None,
                 sys_device: int | None = None,
                 on_status=None,
                 on_file_saved=None,
                 on_idle_timeout=None,
                 on_levels=None):
        self._pa            = pyaudio.PyAudio()
        self._running       = False
        self._mic_q: queue.Queue[bytes | None] = queue.Queue(maxsize=300)
        self._sys_q: queue.Queue[bytes | None] = queue.Queue(maxsize=300)
        self._sys_ok        = False
        self._sys_rate      = SAMPLE_RATE
        self._sys_ch        = CHANNELS
        self._mic_rate      = SAMPLE_RATE
        self._mic_ch        = 1
        self.saved_files:   list[str] = []

        self._silence_rms       = silence_rms   # может меняться извне во время записи
        self._silence_chunks    = max(1, int(silence_duration * SAMPLE_RATE / CHUNK))
        self._min_speech_chunks = max(0, int(min_speech_duration * SAMPLE_RATE / CHUNK))
        self._pre_roll_chunks   = max(1, int(PRE_ROLL_SECS  * SAMPLE_RATE / CHUNK))
        self._post_roll_chunks  = max(1, int(POST_ROLL_SECS * SAMPLE_RATE / CHUNK))
        self._mp3_bitrate       = mp3_bitrate
        self._output_dir        = Path(output_dir)
        self._min_record_secs   = max(0.0, min_record_secs)
        self._idle_timeout_secs = max(0.0, idle_timeout_secs)
        self._output_format     = output_format.lower()
        self._full_record       = full_record
        _fod = Path(full_output_dir) if full_output_dir else self._output_dir
        self._full_output_dir   = _fod
        self._mic_device        = mic_device   # None = auto, int = device index
        self._sys_device        = sys_device   # None = auto, int = device index
        self._on_status         = on_status        # (msg: str) -> None
        self._on_file_saved     = on_file_saved    # (path: str, dur: float) -> None
        self._on_idle_timeout   = on_idle_timeout  # () -> None
        self._on_levels         = on_levels        # (mic_rms: float, sys_rms: float) -> None
        self._mic_muted: bool   = False            # можно менять извне во время записи

    def _emit(self, msg: str) -> None:
        if self._on_status:
            self._on_status(msg)
        else:
            print(msg)

    def _emit_saved(self, path: str, duration_sec: float) -> None:
        if self._on_file_saved:
            self._on_file_saved(path, duration_sec)

    def _emit_levels(self, mic_rms: float, sys_rms: float) -> None:
        if self._on_levels:
            self._on_levels(mic_rms, sys_rms)

    # ── device detection ──────────────────────────────────────────────────────

    def _find_mic(self) -> dict:
        if self._mic_device is not None:
            try:
                return self._pa.get_device_info_by_index(self._mic_device)
            except Exception:
                self._emit(f"[MIC] устройство [{self._mic_device}] не найдено, используется авто")
        try:
            w = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            return self._pa.get_device_info_by_index(w["defaultInputDevice"])
        except Exception:
            return self._pa.get_default_input_device_info()

    def _find_loopback(self) -> dict | None:
        if self._sys_device is not None:
            try:
                return self._pa.get_device_info_by_index(self._sys_device)
            except Exception:
                self._emit(f"[SYSTEM] устройство [{self._sys_device}] не найдено, используется авто")
        try:
            w   = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            spk = self._pa.get_device_info_by_index(w["defaultOutputDevice"])
            for lb in self._pa.get_loopback_device_info_generator():
                if spk["name"] in lb["name"]:
                    return lb
        except Exception:
            pass
        # Fallback: first available loopback (works in Session 0 / SYSTEM context)
        try:
            return next(self._pa.get_loopback_device_info_generator())
        except StopIteration:
            pass
        return None

    # ── stream threads ────────────────────────────────────────────────────────

    def _t_mic(self, stream):
        while self._running:
            try:
                self._mic_q.put(stream.read(CHUNK, exception_on_overflow=False))
            except Exception:
                break
        stream.stop_stream(); stream.close()
        self._mic_q.put(None)

    def _t_sys(self, stream):
        while self._running:
            try:
                self._sys_q.put(stream.read(CHUNK, exception_on_overflow=False))
            except Exception:
                break
        stream.stop_stream(); stream.close()
        self._sys_q.put(None)

    # ── path / save ───────────────────────────────────────────────────────────

    def _make_path(self) -> str:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        ts  = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        ext = 'wav' if self._output_format == 'wav' else 'mp3'
        return str(self._output_dir / f"rec_{ts}.{ext}")

    def _make_full_path(self) -> str:
        self._full_output_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        return str(self._full_output_dir / f"full_{ts}.mp3")

    def _save_mp3(self, frames: list[bytes], path: str) -> None:
        enc = lameenc.Encoder()
        enc.set_bit_rate(self._mp3_bitrate)
        enc.set_in_sample_rate(SAMPLE_RATE)
        enc.set_channels(CHANNELS)
        enc.set_quality(2)
        # Append ~50ms of silence so LAME encoder delay doesn't cut the last word
        lame_pad = bytes(int(SAMPLE_RATE * 0.05) * CHANNELS * 2)
        data = enc.encode(b"".join(frames) + lame_pad) + enc.flush()
        with open(path, "wb") as f:
            f.write(data)
        dur = len(frames) * CHUNK / SAMPLE_RATE
        self._emit(f"  Saved ({dur:.1f}s, {len(data)//1024}KB): {path}")
        self._emit_saved(path, dur)

    def _save_wav(self, frames: list[bytes], path: str) -> None:
        raw = b"".join(frames)
        with _wave.open(path, 'wb') as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)          # 16-bit = 2 bytes
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(raw)
        dur = len(frames) * CHUNK / SAMPLE_RATE
        self._emit(f"  Saved ({dur:.1f}s, {len(raw)//1024}KB): {path}")
        self._emit_saved(path, dur)

    def _save(self, frames: list[bytes], path: str) -> None:
        if self._output_format == 'wav':
            self._save_wav(frames, path)
        else:
            self._save_mp3(frames, path)

    # ── mixer + VAD state machine ─────────────────────────────────────────────
    #
    #  States:  WAITING -> SPEECH -> SILENCE -> WAITING
    #
    #  Pre-roll:        скользящий буфер ~0.25с в WAITING — prepend к первому
    #                   фрейму речи, чтобы не срезать начало слова.
    #
    #  Min record time: пока file_start + min_record_secs не истёк, SILENCE
    #                   не считается (файл не закроется раньше срока).
    #
    #  Idle timeout:    если в WAITING нет звуков дольше idle_timeout_secs,
    #                   запись останавливается полностью.

    def _t_mixer(self):
        WAITING, SPEECH, SILENCE = "waiting", "speech", "silence"
        state        = WAITING
        frames: list[bytes] = []
        silent_count = 0
        sys_buf      = np.zeros(CHUNK * CHANNELS, dtype=np.int16)
        current_path: str | None = None  # set when speech actually starts
        pre_roll: deque[bytes] = deque(maxlen=self._pre_roll_chunks)
        file_start: float | None = None
        last_sound: float = time.monotonic()
        _level_tick  = 0
        _sys_rms     = 0.0

        def _min_elapsed() -> bool:
            return (self._min_record_secs == 0.0 or
                    file_start is None or
                    time.monotonic() - file_start >= self._min_record_secs)

        self._emit(f"  Ожидание речи (порог RMS: {self._silence_rms})...")

        # ── Full continuous recording setup ───────────────────────────────────
        full_path      = None
        full_enc       = None   # lameenc.Encoder  (MP3 mode)
        full_wav       = None   # wave.Wave_write  (WAV mode)
        full_fh        = None   # raw file handle  (MP3 mode)
        full_frames    = 0      # frame counter for duration

        if self._full_record:
            full_path = self._make_full_path()
            full_enc = lameenc.Encoder()
            full_enc.set_bit_rate(self._mp3_bitrate)
            full_enc.set_in_sample_rate(SAMPLE_RATE)
            full_enc.set_channels(CHANNELS)
            full_enc.set_quality(2)
            full_fh = open(full_path, 'wb')
            self._emit(f"  [FULL] {full_path}")

        while True:
            mic_raw = self._mic_q.get()
            if mic_raw is None:
                break

            mic_arr = _to_stereo(mic_raw, self._mic_ch)
            if self._mic_rate != SAMPLE_RATE:
                mic_arr = _resample(mic_arr, self._mic_rate)

            mic_rms = _rms(mic_arr)   # реальный уровень — всегда для метра
            if self._mic_muted:
                mic_arr = np.zeros_like(mic_arr)   # не пишем в микс, но VAD по sys продолжает работать

            if self._sys_ok:
                try:
                    sys_raw = self._sys_q.get_nowait()
                    if sys_raw is not None:
                        sys_buf  = _resample(_to_stereo(sys_raw, self._sys_ch), self._sys_rate)
                        _sys_rms = _rms(sys_buf[:len(mic_arr)])
                except queue.Empty:
                    pass

            mixed = _mix(mic_arr, sys_buf[:len(mic_arr)])
            level = _rms(mixed)

            # ── Full continuous recording ─────────────────────────────────────
            if self._full_record:
                full_frames += 1
                chunk_mp3 = full_enc.encode(mixed.tobytes())
                if chunk_mp3:
                    full_fh.write(chunk_mp3)

            # emit meter levels ~8 times/sec (every 6 chunks ≈ 125ms)
            _level_tick += 1
            if _level_tick >= 6:
                _level_tick = 0
                self._emit_levels(mic_rms, _sys_rms)

            if level >= self._silence_rms:
                last_sound = time.monotonic()

            # ── WAITING ───────────────────────────────────────────────────────
            if state == WAITING:
                if (self._idle_timeout_secs > 0.0 and
                        time.monotonic() - last_sound > self._idle_timeout_secs):
                    self._emit("  [IDLE] Нет звуков слишком долго, остановка.")
                    self._running = False
                    if self._on_idle_timeout:
                        self._on_idle_timeout()
                    break

                pre_roll.append(mixed.tobytes())
                if level >= self._silence_rms:
                    state = SPEECH
                    current_path = self._make_path()  # timestamp = actual speech start
                    frames = list(pre_roll)   # prepend pre-roll to capture onset
                    file_start = time.monotonic()
                    self._emit(f"  [REC] {current_path}")

            # ── SPEECH ────────────────────────────────────────────────────────
            elif state == SPEECH:
                frames.append(mixed.tobytes())
                if level < self._silence_rms and _min_elapsed():
                    state = SILENCE
                    silent_count = 0

            # ── SILENCE ───────────────────────────────────────────────────────
            elif state == SILENCE:
                frames.append(mixed.tobytes())
                if level >= self._silence_rms:
                    state = SPEECH
                    silent_count = 0
                elif _min_elapsed():
                    silent_count += 1
                    if silent_count >= self._silence_chunks:
                        # Keep post-roll tail so word endings (low-energy consonants) aren't cut
                        keep = min(silent_count, self._post_roll_chunks)
                        trim = silent_count - keep
                        speech_frames = frames[:-trim] if trim > 0 else frames
                        if len(speech_frames) >= self._min_speech_chunks:
                            self._save(speech_frames, current_path)
                            self.saved_files.append(current_path)
                        frames = []
                        silent_count = 0
                        state = WAITING
                        file_start = None
                        current_path = None  # will be set when next speech starts
                        pre_roll.clear()
                        self._emit("  Ожидание речи...")

        # End of recording — save whatever is left
        if frames and current_path:
            sc   = silent_count if state == SILENCE else 0
            keep = min(sc, self._post_roll_chunks)
            trim = sc - keep
            speech_frames = frames[:-trim] if trim > 0 else frames
            if len(speech_frames) >= self._min_speech_chunks:
                self._save(speech_frames, current_path)
                self.saved_files.append(current_path)

        # ── Finalize full continuous recording (last in list) ─────────────────
        if self._full_record and full_path and full_enc and full_fh:
            dur = full_frames * CHUNK / SAMPLE_RATE
            try:
                lame_pad = bytes(int(SAMPLE_RATE * 0.05) * CHANNELS * 2)
                tail = full_enc.encode(lame_pad) + full_enc.flush()
                if tail:
                    full_fh.write(tail)
                full_fh.close()
                size_kb = Path(full_path).stat().st_size // 1024
                self._emit(f"  [FULL] Saved ({dur:.1f}s, {size_kb}KB): {full_path}")
                self._emit_saved(full_path, dur)
                self.saved_files.append(full_path)
            except Exception as e:
                self._emit(f"  [FULL] Error saving: {e}")

    # ── public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self.saved_files = []

        mic_info   = self._find_mic()
        mic_ch     = min(int(mic_info["maxInputChannels"]), 2) or 1
        mic_native = int(mic_info["defaultSampleRate"])
        mic_stream = None
        for rate in dict.fromkeys([SAMPLE_RATE, mic_native]):
            try:
                mic_stream = self._pa.open(
                    format=FORMAT, channels=mic_ch, rate=rate,
                    input=True, input_device_index=int(mic_info["index"]),
                    frames_per_buffer=CHUNK,
                )
                self._mic_rate = rate
                self._mic_ch   = mic_ch
                break
            except Exception:
                mic_stream = None
        if mic_stream is None:
            raise RuntimeError(f"Не удалось открыть микрофон '{mic_info['name']}' "
                               f"ни на {SAMPLE_RATE}Hz, ни на {mic_native}Hz")
        self._emit(f"[MIC]    {mic_info['name']}  ({mic_ch}ch, {self._mic_rate}Hz)")

        lb_info = self._find_loopback()
        sys_stream = None
        if lb_info:
            self._sys_rate = int(lb_info["defaultSampleRate"])
            self._sys_ch   = int(lb_info["maxInputChannels"])
            try:
                sys_stream = self._pa.open(
                    format=FORMAT, channels=self._sys_ch, rate=self._sys_rate,
                    input=True, input_device_index=int(lb_info["index"]),
                    frames_per_buffer=CHUNK,
                )
                self._sys_ok = True
                self._emit(f"[SYSTEM] {lb_info['name']}  ({self._sys_ch}ch, {self._sys_rate}Hz)")
            except Exception as e:
                self._emit(f"[SYSTEM] loopback failed: {e} -- mic only")
        else:
            self._emit("[SYSTEM] no loopback found -- mic only")

        threading.Thread(target=self._t_mic, args=(mic_stream,), daemon=True).start()
        if sys_stream:
            threading.Thread(target=self._t_sys, args=(sys_stream,), daemon=True).start()
        self._mixer_t = threading.Thread(target=self._t_mixer, daemon=True)
        self._mixer_t.start()

    def stop(self) -> list[str]:
        self._running = False
        self._mixer_t.join(timeout=10)
        self._pa.terminate()
        return self.saved_files


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Silence: RMS<{SILENCE_RMS} for {SILENCE_DURATION}s = {int(SILENCE_DURATION * SAMPLE_RATE / CHUNK)} chunks")

    rec = Recorder()
    rec.start()

    duration = float(sys.argv[1]) if len(sys.argv) > 1 else None
    if duration:
        print(f"Recording for {duration}s ...")
        time.sleep(duration)
    else:
        print("Recording ... press Enter to stop.")
        input()

    files = rec.stop()
    print(f"\nDone. {len(files)} file(s):")
    for f in files:
        print(f"  {f}")

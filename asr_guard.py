"""CPU-only decoding policy, durable chunk checkpoints and a hard GPU-call deadline."""
import math
from pathlib import Path
import os
import threading
import time

from pipeline_common import atomic_json, now, read_json


RETRY_CHUNK = 42
DECODER_EXHAUSTED = 43


class DecoderExhausted(RuntimeError):
    pass


def decoder_policy(cfg):
    policy = {'beam_size': int(cfg.get('asr_beam_size', 1)),
              'timeout_factor': float(cfg.get('asr_chunk_timeout_factor', 8)),
              'timeout_min_sec': float(cfg.get('asr_chunk_timeout_min_sec', 60)),
              'timeout_max_sec': float(cfg.get('asr_chunk_timeout_max_sec', 1800))}
    if (policy['beam_size'] < 1 or
            any(not math.isfinite(v) or v <= 0 for v in policy.values()) or
            policy['timeout_max_sec'] < policy['timeout_min_sec']):
        raise ValueError('解码参数无效；不会以无超时保护的方式启动转写')
    return policy


def chunk_deadline(duration, policy):
    return min(policy['timeout_max_sec'],
               max(policy['timeout_min_sec'], duration * policy['timeout_factor']))


def decode_options(language, beam_size):
    # Whisper's default temperature tuple can silently try sampling several more times.
    # beam_size=None selects its actual GreedyDecoder; beam_size=1 is still BeamSearchDecoder.
    return dict(language=language, task='transcribe', fp16=True, temperature=0.0,
                beam_size=None if beam_size == 1 else beam_size, best_of=None,
                condition_on_previous_text=False)


class ChunkProgress:
    def __init__(self, path, binding):
        self.path = Path(path)
        previous = read_json(self.path)
        self.data = (previous if previous.get('schema') == 1 and previous.get('binding') == binding
                     else {'schema': 1, 'binding': binding, 'chunks': {}, 'timeouts': []})

    def record(self, number):
        return self.data['chunks'].get(str(number))

    def failures(self, number):
        return [x for x in self.data['timeouts'] if x['chunk'] == number]

    def beam_for(self, number, configured):
        if len(self.failures(number)) >= 2:
            raise DecoderExhausted('片段%d已在超时后用贪心重试仍超时；保留进度并标记待处理，不再无限重跑' % (number + 1))
        return 1 if self.failures(number) else configured

    def timed_out(self, number, start, end, beam_size, deadline):
        self.data['timeouts'].append({'chunk': number, 'start': start, 'end': end,
            'beam_size': beam_size, 'timeout_sec': deadline, 'at': now().isoformat()})
        atomic_json(self.path, self.data)
        return RETRY_CHUNK if len(self.failures(number)) == 1 else DECODER_EXHAUSTED

    def completed(self, number, start, end, segments, review, beam_size, elapsed, peak_gpu_gb):
        self.data['chunks'][str(number)] = {'start': start, 'end': end, 'segments': segments,
            'review': review, 'beam_size': beam_size, 'elapsed_sec': round(elapsed, 2),
            'peak_gpu_gb': peak_gpu_gb}
        atomic_json(self.path, self.data)


def guarded_decode(operation, timeout, on_timeout, label, heartbeat_sec=30):
    """A Python thread cannot cancel CUDA. Exit this lecture, then let the parent restart it.

    The timeout checkpoint is committed BEFORE exit; earlier chunks remain reusable.
    The parent's Windows Job kills descendants and its next worker starts with clean VRAM.
    """
    finished = threading.Event()
    gate = threading.Lock()
    started = time.monotonic()

    def expire():
        print('%s 解码超时；提交片段断点并退出，释放本课资源' % label, flush=True)
        try:
            code = on_timeout()
        except Exception as exc:
            print('超时断点保存失败：%s' % type(exc).__name__, flush=True)
            code = DECODER_EXHAUSTED
        os._exit(code)

    def watch():
        while True:
            remaining = timeout - (time.monotonic() - started)
            if finished.wait(max(0, min(heartbeat_sec, remaining))):
                return
            with gate:
                if finished.is_set():
                    return
                elapsed = time.monotonic() - started
                if elapsed >= timeout:
                    expire()
                print('%s 解码中：已用%.0f秒 / 最多%.0f秒' % (label, elapsed, timeout), flush=True)

    watcher = threading.Thread(target=watch, name='decoder-deadline', daemon=True)
    watcher.start()
    try:
        result = operation()
        with gate:
            if time.monotonic() - started >= timeout:
                expire()
            finished.set()
        return result
    finally:
        finished.set()
        watcher.join(timeout=2)

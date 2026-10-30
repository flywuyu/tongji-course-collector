"""One lecture per GPU process, measured silence, original video time preserved."""
from hidden_process import run as run_hidden
import argparse
import gc
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

import config as C
from pipeline_common import (atomic_json, atomic_text, audio_done, course_rule, items,
                             lecture_path, now, sha, skip_audio, window,
                             subtitle_status, settings, lock)
from asr_guard import (ChunkProgress, DecoderExhausted, DECODER_EXHAUSTED,
                       decoder_policy, chunk_deadline, decode_options, guarded_decode)
from model_loader import load_recognition_model


class MediaUnavailable(RuntimeError):
    pass


def media_probe(source):
    """Bound every network read; a transient timeout is not evidence of a silent class."""
    remote = str(source).startswith(('http://', 'https://'))
    attempts = 3 if remote else 1
    for attempt in range(attempts):
        try:
            return _media_probe_once(source, remote)
        except (subprocess.TimeoutExpired, OSError, ValueError, RuntimeError) as exc:
            if attempt + 1 == attempts:
                raise MediaUnavailable('媒体探测未成功（已进行%d次有界尝试），应刷新地址后重试：%s' %
                                       (attempts, type(exc).__name__)) from None
            print('媒体探测暂时失败，短暂等待后重试 %d/%d' % (attempt+2, attempts), flush=True)
            time.sleep(1)


def _media_probe_once(source, remote):
    ffprobe = str(Path(C.FFDIR) / 'ffprobe.exe')
    network = ['-rw_timeout', '10000000', '-user_agent', 'Mozilla/5.0'] if remote else []
    limits = ['-probesize', '1048576', '-analyzeduration', '5000000']
    if not Path(ffprobe).exists():
        proc = run_hidden([C.FF, '-hide_banner', *network, *limits, '-i', str(source)], capture_output=True,
                              encoding='utf-8', errors='strict', timeout=25 if remote else 30)
        text = proc.stderr
        match = re.search(r'Duration:\s*(\d+):(\d+):(\d+\.?\d*)', text)
        streams = [{'codec_type': name.lower()} for name in ('Audio', 'Video')
                   if re.search(r'Stream .*?: ' + name + ':', text)]
        if not match or not streams:
            raise RuntimeError('媒体探测失败（网络或地址过期），不能当成静音')
        duration = int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])
        return duration, streams
    cmd = [ffprobe, '-v', 'error', *network, *limits, '-show_entries', 'format=duration:stream=codec_type',
           '-of', 'json', str(source)]
    proc = run_hidden(cmd, capture_output=True, encoding='utf-8', errors='strict', timeout=25 if remote else 30)
    if proc.returncode:
        raise RuntimeError('媒体探测失败（网络或地址过期），不能当成静音')
    data = json.loads(proc.stdout)
    return float(data.get('format', {}).get('duration') or 0), data.get('streams', [])


def analyze_audio(wav, duration, noise=-45):
    proc = run_hidden([C.FF, '-hide_banner', '-nostats', '-threads', '2', '-i', str(wav),
              '-af', 'silencedetect=noise=%sdB:d=30,volumedetect' % noise, '-f', 'null', os.devnull],
              capture_output=True, encoding='utf-8', errors='strict', timeout=240)
    if proc.returncode:
        raise RuntimeError('静音检测失败，保留音频等待重试')
    out, start = [], None
    for line in proc.stderr.splitlines():
        a = re.search(r'silence_start:\s*([\d.]+)', line)
        b = re.search(r'silence_end:\s*([\d.]+)', line)
        if a:
            start = float(a.group(1))
        if b and start is not None:
            out.append([start, min(duration, float(b.group(1)))])
            start = None
    if start is not None:
        out.append([start, duration])
    peak = re.search(r'max_volume:\s*([\-\d.]+) dB', proc.stderr)
    peak = float(peak.group(1)) if peak else None
    return out, peak


def measured_silences(wav, duration):
    return analyze_audio(wav, duration)[0]


def speech_ranges(duration, silences, max_chunk=900):
    """Only remove measured long silences; retain two seconds at each speech boundary."""
    ranges, cursor = [], 0.0
    for start, end in sorted(silences):
        stop = 0 if start <= .1 else min(duration, start + 2)
        if stop > cursor:
            ranges.append([cursor, stop])
        cursor = max(cursor, duration if end >= duration - .1 else end - 2)
    if cursor < duration:
        ranges.append([cursor, duration])
    chunks = []
    for start, end in ranges:
        while end - start > max_chunk:
            chunks.append([start, start + max_chunk])
            start += max_chunk
        if end - start > .2:
            chunks.append([start, end])
    return chunks


def fmt(sec, srt=False):
    if srt:
        millis = max(0, round(sec * 1000))
        return '%02d:%02d:%02d,%03d' % (millis // 3600000, millis // 60000 % 60,
                                        millis // 1000 % 60, millis % 1000)
    sec = max(0, int(sec))
    return '%02d:%02d:%02d' % (sec // 3600, sec // 60 % 60, sec % 60)


def normalized_asr_text(raw):
    text=re.sub(r'\s+',' ',raw or '').strip()
    damaged='\ufffd' in text
    return text.replace('\ufffd','[识别不清]'),damaged


def commit_subtitles(it, outdir, segments, meta):
    if not segments:
        raise RuntimeError('没有有效字幕；不会将空结果标为完成')
    txt = ''.join('[%s] %s\n' % (fmt(x['start']), x['text']) for x in segments)
    srt = ''.join('%d\n%s --> %s\n%s\n\n' %
                  (i, fmt(x['start'], True), fmt(x['end'], True), x['text'])
                  for i, x in enumerate(segments, 1))
    outdir.mkdir(parents=True, exist_ok=True)
    atomic_json(outdir / 'commit.pending', {'sub_id': it['sub_id'], 'at': now().isoformat()})
    # Commit marker comes last and binds both files. A crash between replaces is detectable.
    atomic_text(outdir / 'transcript.txt', txt)
    atomic_text(outdir / 'transcript.srt', srt)
    atomic_json(outdir / 'done.json', {**meta, 'schema': 2, 'sub_id': it['sub_id'],
                'course_id': it['course_id'], 'title': it['title'], 'date': it['date'],
                'segments': len(segments), 'finished_at': now().isoformat(),
                'hashes': {'txt': sha(outdir / 'transcript.txt'), 'srt': sha(outdir / 'transcript.srt')}})
    (outdir / 'commit.pending').unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sub-id', required=True)
    parser.add_argument('--download-only', action='store_true', help='仅下载及核验音频；不导入GPU模型')
    args = parser.parse_args()
    it = next(x for x in items() if str(x['sub_id']) == args.sub_id)
    if audio_done(it) or skip_audio(it):
        print('已完成或免字幕，跳过')
        return 0
    if subtitle_status(it).startswith('待核对'):
        print('字幕存在外部修改，保留当前文件，不自动重录覆盖')
        return 32
    if not window()[0]:
        print('当前不在00:00–08:00的允许窗口，不启动转写')
        return 30
    if args.download_only:
        try:
            wav = ensure_audio(it)
        except MediaUnavailable as exc:
            print(str(exc), flush=True)
            return 21
        print('音频预取完成，已核验' if wav else '免音频，预取跳过', flush=True)
        return 0
    import runlock
    ok, _ = runlock.acquire()
    if not ok:
        return 31
    try:
        try:
            return transcribe(it)
        except DecoderExhausted as exc:
            print(str(exc), flush=True)
            return DECODER_EXHAUSTED
        except MediaUnavailable as exc:
            print(str(exc), flush=True)
            return 21
    finally:
        runlock.release()


def ensure_audio(it):
    """One writer per lecture, shared by the foreground ASR and the parent's prefetch."""
    from audio_download import download_audio, valid_audio
    if skip_audio(it):
        return None
    root = lecture_path(it)
    wav = root / 'audio' / 'lecture.wav'
    with lock('audio-' + str(it['sub_id'])):
        expected = float(it.get('duration') or 0)
        if not valid_audio(str(wav), expected):
            actual, streams = media_probe(it['playurl'])
            if not any(x.get('codec_type') == 'audio' for x in streams):
                atomic_json(root / 'media.json', {'sub_id': it['sub_id'], 'audio_status': 'silent',
                            'reason': '媒体没有音轨', 'duration_sec': actual, 'checked_at': now().isoformat()})
                return None
            ok, message = download_audio(it['playurl'], str(wav), actual or expected)
            if not ok:
                raise MediaUnavailable('音频下载或时长校验失败，保留缓存并刷新地址后重试')
    return wav


def transcribe(it):
    root = lecture_path(it)
    wav = ensure_audio(it)
    if wav is None:
        return 0
    audio_dir = (root / 'audio').resolve()
    for stale in audio_dir.glob('asr_*'):
        if stale.is_dir() and stale.resolve().parent == audio_dir:
            shutil.rmtree(stale.resolve())
    duration, _ = media_probe(str(wav))
    print('音频已就绪：实测%.1f秒；开始静音分析' % duration, flush=True)
    silences, peak = analyze_audio(wav, duration)
    if peak is not None and -75 < peak < -35:
        silences, peak = analyze_audio(wav, duration, max(-90, peak - 25))
    silent_sec = sum(max(0, b - a) for a, b in silences)
    media = {'sub_id': it['sub_id'], 'duration_sec': duration, 'audio_status': 'present',
             'silences': silences, 'peak_db': peak, 'rule': course_rule(it), 'checked_at': now().isoformat()}
    # Require both almost-complete silence and negligible remaining speech; no timetable guesses.
    if silent_sec >= duration * .995 and duration - silent_sec < 10 and peak is not None and peak <= -75:
        media.update(audio_status='silent', reason='实测全程无声')
        atomic_json(root / 'media.json', media)
        wav.unlink(missing_ok=True)
        return 0
    ranges = speech_ranges(duration, silences)
    if not ranges:
        ranges = speech_ranges(duration, [])
    media['processed_ranges'] = ranges
    atomic_json(root / 'media.json', media)
    policy = decoder_policy(settings())
    language = course_rule(it).get('language', 'zh')
    progress = ChunkProgress(root / 'audio' / 'asr_progress.json',
                {'sub_id': str(it['sub_id']), 'audio_sha256': sha(wav), 'ranges': ranges,
                 'model': 'large-v3', 'language': language, 'decoding_version': 1,
                 'configured_beam_size': policy['beam_size']})
    # Exhausted chunks are reported before paying model-load time on subsequent batches.
    for number in range(len(ranges)):
        if not progress.record(number):
            progress.beam_for(number, policy['beam_size'])
    os.environ['PATH'] = C.FFDIR + os.pathsep + os.environ.get('PATH', '')
    import torch
    torch.set_num_threads(2)
    def load_timeout():
        atomic_json(root/'audio'/'model_load_failure.json',
                    {'at':now().isoformat(),'sub_id':str(it['sub_id']),'reason':'模型加载超过180秒'})
        return 44
    model = guarded_decode(lambda:load_recognition_model('large-v3'), 180, load_timeout, '模型加载')
    torch.cuda.reset_peak_memory_stats()
    segments, review = [], []
    with tempfile.TemporaryDirectory(prefix='asr_', dir=root / 'audio') as temp:
        for number, (start, end) in enumerate(ranges):
            if not window()[0]:
                return 30
            saved = progress.record(number)
            if saved:
                segments.extend(saved['segments'])
                review.extend(saved['review'])
                print('复用已完成片段 %d/%d；原视频 %s–%s' %
                      (number + 1, len(ranges), fmt(start), fmt(end)), flush=True)
                continue
            chunk = Path(temp) / 'chunk.wav'
            run_hidden([C.FF, '-y', '-v', 'error', '-threads', '2', '-ss', str(start),
                '-i', str(wav), '-t', str(end - start), '-ac', '1', '-ar', '16000', str(chunk)],
                check=True, timeout=120, capture_output=True)
            beam = progress.beam_for(number, policy['beam_size'])
            deadline = chunk_deadline(end - start, policy)
            label = '片段%d/%d %s–%s' % (number + 1, len(ranges), fmt(start), fmt(end))
            print('%s 开始：beam_size=%d，超时上限%.0f秒' % (label, beam, deadline), flush=True)
            started = time.monotonic()
            result = guarded_decode(
                lambda: model.transcribe(str(chunk), **decode_options(language, beam)), deadline,
                lambda: progress.timed_out(number, start, end, beam, deadline), label)
            chunk_segments, chunk_review = [], []
            for row in result.get('segments', []):
                text,damaged = normalized_asr_text(row.get('text',''))
                if not text:
                    continue
                rec = {'start': min(end, start + max(0, row['start'])),
                       'end': min(end, start + max(0, row['end'])), 'text': text,
                       'no_speech_prob': row.get('no_speech_prob', 0),
                       'avg_logprob': row.get('avg_logprob', 0)}
                if rec['end'] <= rec['start']:
                    continue
                if damaged:
                    chunk_review.append({'start':rec['start'],'text':text,'issue':'invalid_unicode',
                                   'raw_token_ids':[int(x)for x in row.get('tokens',[])]})
                from clean_halluc import is_halluc
                if is_halluc(text):
                    chunk_review.append({'start': rec['start'], 'text': text})
                chunk_segments.append(rec)
            progress.completed(number, start, end, chunk_segments, chunk_review, beam,
                time.monotonic() - started, round(torch.cuda.max_memory_allocated() / 1e9, 2))
            segments.extend(chunk_segments)
            review.extend(chunk_review)
            del result
            gc.collect()
            torch.cuda.empty_cache()
            chunk.unlink(missing_ok=True)
            print('已转写片段 %d/%d；原视频 %s–%s' %
                  (number + 1, len(ranges), fmt(start), fmt(end)), flush=True)
    segments.sort(key=lambda x: x['start'])
    commit_subtitles(it, root / 'transcript', segments,
                     {'model': 'large-v3', 'duration_sec': duration, 'silences': silences,
                      'beam_size': policy['beam_size'], 'decoder_policy': policy,
                      'decoder_fallback': bool(progress.data['timeouts']),
                      'decoder_timeouts': progress.data['timeouts'],
                      'chunk_decoders': [{'chunk': int(n), **{k: row[k] for k in
                          ('start', 'end', 'beam_size', 'elapsed_sec')}}
                          for n, row in sorted(progress.data['chunks'].items(), key=lambda x: int(x[0]))],
                      'processed_ranges': ranges, 'review_candidates': review,
                      'segment_meta': [{k: s[k] for k in ('start', 'end', 'no_speech_prob', 'avg_logprob')}
                                       for s in segments],
                      'peak_gpu_gb': max([round(torch.cuda.max_memory_allocated() / 1e9, 2)] +
                          [row['peak_gpu_gb'] for row in progress.data['chunks'].values()])})
    del model
    gc.collect()
    torch.cuda.empty_cache()
    wav.unlink(missing_ok=True)
    print('字幕已提交；退出本课次进程释放全部资源', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

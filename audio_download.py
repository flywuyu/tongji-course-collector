"""Atomic audio download and measured duration validation; no GPU imports."""
import os,re,subprocess,time
import config as C
FF=C.FF

def probe_duration(path):
    r = subprocess.run([FF, '-hide_banner', '-i', path],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    m = re.search(r'Duration:\s*(\d+):(\d+):(\d+\.?\d*)', r.stderr or '')
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

def download_audio(url, dst, expect_sec):
    expect_sec = float(expect_sec or 0)
    tmp = dst + '.tmp'
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(tmp):
        # A stopped ffmpeg can leave a plausible-duration WAV. No successful return code
        # was committed for this temporary file, so it must never be promoted as complete.
        try:
            os.remove(tmp)
        except Exception:
            pass

    cmd = [FF, '-y', '-hide_banner', '-loglevel', 'error',
           '-rw_timeout', '30000000',
           '-user_agent', 'Mozilla/5.0',
           '-i', url, '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le',
           '-t', str(int(float(expect_sec) + 60)), '-f', 'wav', tmp]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='strict',
                           timeout=max(600, int(expect_sec / 3)))
    except subprocess.TimeoutExpired:
        return False, '音频下载超时，保留临时文件，下次核验后续跑'

    if r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 100000:
        return False, 'ffmpeg 失败: ' + (r.stderr or '')[-200:]
    if expect_sec and expect_sec > 60:
        got = probe_duration(tmp)
        if got <= 0 or got < expect_sec * 0.95:
            return False, '音频不完整：%.0f 秒 / 应为 %.0f 秒' % (got, expect_sec)

    os.replace(tmp, dst)
    return True, '%.1f MB / 用时 %.0f 秒' % (os.path.getsize(dst) / 1e6, time.time() - t0)

def valid_audio(path, expect_sec):
    if not os.path.exists(path) or os.path.getsize(path) <= 100000:
        return False
    got = probe_duration(path)
    return got > 0 and got >= float(expect_sec or 0) * 0.95

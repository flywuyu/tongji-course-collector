"""Preflight probes resources, checkpoint and media, without loading a model on GPU."""
import json
from pathlib import Path
import psutil
from pipeline_common import STATE, atomic_json, audio_done, items, lecture_path, now, settings


def main():
    from model_loader import checkpoint_file
    from transcribe_one import media_probe
    from workflow import gpu_free_gb
    cfg=settings()
    problems=[]
    report={'at':now().isoformat(),'gpu_model_loaded':False,
            'available_ram_gb':round(psutil.virtual_memory().available/1e9,2),
            'gpu_free_gb':gpu_free_gb()}
    try:
        report['checkpoint']=str(checkpoint_file())
    except Exception as exc:
        problems.append(str(exc))
    if report['available_ram_gb']<cfg['min_available_ram_gb']:
        problems.append('可用内存不足，夜间需等待资源恢复')
    if report['gpu_free_gb'] is None or report['gpu_free_gb']<cfg.get('asr_min_gpu_free_gb',4.5):
        problems.append('独立显存暂时不足，夜间将等待，不强行使用共享内存')
    pending=[it for it in items() if not audio_done(it)]
    if pending:
        it=pending[0]
        wav=lecture_path(it)/'audio'/'lecture.wav'
        try:
            duration,streams=media_probe(str(wav) if wav.exists() else it['playurl'])
            report['media_probe']={'sub_id':str(it['sub_id']),'duration_sec':duration,
                                   'has_audio':any(s.get('codec_type')=='audio' for s in streams)}
        except Exception as exc:
            problems.append(str(exc))
    report.update(problems=problems,ok=not problems)
    atomic_json(STATE/'healthcheck.json',report)
    print(json.dumps(report,ensure_ascii=False),flush=True)
    return 0 if report['ok'] else 40


if __name__=='__main__':
    raise SystemExit(main())

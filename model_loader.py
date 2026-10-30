"""Load the existing Whisper weights directly into FP16, without a FP32 GPU copy."""
import gc
import hashlib
import os
from pathlib import Path
import time

from pipeline_common import STATE, atomic_json, now, read_json


def checkpoint_file(name='large-v3'):
    import whisper
    root = Path(os.getenv('XDG_CACHE_HOME', str(Path.home()/'.cache'))) / 'whisper'
    source = whisper._MODELS[name]
    path = root / source.rsplit('/', 1)[-1]
    if not path.is_file():
        raise RuntimeError('本机缺少识别模型，请先完成模型准备；不在夜间加载时隐式下载')
    expected = source.split('/')[-2]
    stat = path.stat()
    signature = {'path': str(path), 'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
                 'expected_sha256': expected}
    cache = read_json(STATE/'model_checkpoint.json')
    if cache.get('signature') != signature or cache.get('sha256') != expected:
        digest = hashlib.sha256()
        with open(path, 'rb') as f:
            for block in iter(lambda:f.read(4*1024*1024), b''):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise RuntimeError('识别模型完整性核验失败；不会使用损坏权重')
        atomic_json(STATE/'model_checkpoint.json', {'signature': signature,
                    'sha256': expected, 'verified_at': now().isoformat()})
    return path


def load_recognition_model(name='large-v3', device='cuda', path=None):
    import torch
    import whisper
    from whisper.model import LayerNorm, ModelDimensions, Whisper
    import psutil
    started = time.monotonic()
    path = Path(path) if path else checkpoint_file(name)
    print('加载模型：CPU映射权重，直接构造FP16；避免先加载FP32到显卡', flush=True)
    checkpoint = torch.load(str(path), map_location='cpu', mmap=True, weights_only=True)
    dims = ModelDimensions(**checkpoint['dims'])
    original_dtype = torch.get_default_dtype()
    try:
        torch.set_default_dtype(torch.float16)
        with torch.device(device):
            model = Whisper(dims)
    finally:
        torch.set_default_dtype(original_dtype)
    # Copy one tensor at a time from CPU's memory-mapped checkpoint to the FP16 model.
    # Never load the whole checkpoint onto CUDA alongside a second, FP32 model.
    model.load_state_dict(checkpoint['model_state_dict'], strict=True)
    alignment = whisper._ALIGNMENT_HEADS.get(name)
    if alignment is not None:
        model.set_alignment_heads(alignment)
        model.alignment_heads = model.alignment_heads.to(device)
    del checkpoint
    gc.collect()
    model.half()
    for module in model.modules():
        if isinstance(module, LayerNorm):
            module.float()
    model.eval()
    gpu_gb = torch.cuda.memory_allocated()/1e9 if str(device).startswith('cuda') else 0
    print('模型就绪：用时%.1f秒；进程内存%.2fGB；显存驻留%.2fGB' %
          (time.monotonic()-started, psutil.Process().memory_info().rss/1e9, gpu_gb), flush=True)
    return model

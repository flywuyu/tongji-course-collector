"""Windows DPAPI: encrypted for the current Windows user, never plaintext on disk."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path

from pipeline_common import STATE,atomic_bytes

PATH = STATE / 'account.dpapi'


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def crypt(data, decrypt=False):
    buf = ctypes.create_string_buffer(data)
    src = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    out = Blob()
    api = ctypes.WinDLL('crypt32', use_last_error=True)
    if decrypt:
        ok = api.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(out))
    else:
        ok = api.CryptProtectData(ctypes.byref(src), 'Tongji course workflow', None, None, None, 1,
                                  ctypes.byref(out))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out.data, out.size)
    finally:
        free = ctypes.WinDLL('kernel32').LocalFree
        free.argtypes = [ctypes.c_void_p]
        free(ctypes.cast(out.data, ctypes.c_void_p))


def save(username, password):
    STATE.mkdir(exist_ok=True)
    encrypted = crypt(json.dumps({'username': username, 'password': password}).encode('utf-8'))
    atomic_bytes(PATH,encrypted)


def load():
    if not PATH.exists():
        return None
    return json.loads(crypt(PATH.read_bytes(), decrypt=True).decode('utf-8'))

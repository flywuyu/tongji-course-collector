"""Local Windows reminder. Auto-closes; works without msg.exe or extra software."""
import ctypes
from ctypes import wintypes
import sys


def show(message, milliseconds=120000):
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    func = user32.MessageBoxTimeoutW
    func.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                     wintypes.UINT, wintypes.WORD, wintypes.DWORD]
    func.restype = ctypes.c_int
    # No system-modal flag; the user's other applications remain usable.
    result = func(None, message, '同济课程采集提醒', 0x40, 0, milliseconds)
    if result == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    return result


if __name__ == '__main__':
    show(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 120000)

"""Create background console tools without allocating a visible Windows console."""
import os
import subprocess


def options(kwargs):
    result = dict(kwargs)
    if os.name == 'nt':
        flags = result.get('creationflags', 0)
        # CREATE_NEW_CONSOLE/DETACHED_PROCESS would make CREATE_NO_WINDOW ineffective.
        flags &= ~(subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS)
        result['creationflags'] = flags | subprocess.CREATE_NO_WINDOW
        startup = result.get('startupinfo') or subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        result['startupinfo'] = startup
    return result


def run(*args, **kwargs):
    return subprocess.run(*args, **options(kwargs))


def call(*args, **kwargs):
    return run(*args, **kwargs).returncode

"""Use UTF-8 for Chinese command output, including redirected non-Chinese Windows logs."""
import sys


def configure():
    for stream in (sys.stdout,sys.stderr):
        if stream is not None and hasattr(stream,'reconfigure'):
            stream.reconfigure(encoding='utf-8',errors='strict')

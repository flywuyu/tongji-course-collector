"""PPT URL pattern; actual probing is owned by platform_session."""
import re
PAT = re.compile(r'(https://look\.tongji\.edu\.cn/play/ai/clip/ppt_extraction/[^"\'\s\\]+/ppt/)')

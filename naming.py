# -*- coding: utf-8 -*-
"""统一的目录命名规则

格式：  W{周次}_{月-日}_{节次}
例子：  W04_10-21_第1-2节
        W01_09-14_第3-4节
        W05_10-13          （取不到节次时退化成这个）

排序按名字就是时间顺序；周次从学期开始日算。
"""
import re
import datetime
import config as C




def week_of(date_str):
    try:
        d = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
    except Exception:
        return 0
    if not C.TERM_START:
        return 0
    start=datetime.date.fromisoformat(C.TERM_START)
    return (d - start).days // 7 + 1


def slot_of(sub_title):
    """从 '2026-10-21第1-2节' 里抠出 '第1-2节'"""
    m = re.search(r'第\s*\d+(?:\s*[-~]\s*\d+)?\s*节', sub_title or '')
    return m.group(0).replace(' ', '') if m else ''


def lecture_dir(it):
    """给一条课次记录，返回目录名"""
    date_str = it.get('date') or ''
    wk = week_of(date_str)
    md = date_str[5:].replace('-', '-') if len(date_str) >= 10 else 'unknown'
    slot = slot_of(it.get('sub_title') or '')
    name = 'W%02d_%s' % (wk, md)
    if slot:
        name += '_' + slot
    # 同一天同一节次理论上不会有两条，防御性处理
    return name


if __name__ == '__main__':
    import json, sys
    import config as C
    p = sys.argv[1] if len(sys.argv) > 1 else C.CAT
    items = json.load(open(p, encoding='utf-8'))['items']
    for it in items[:12]:
        print('%-12s %-14s -> %s' % (it['date'], (it.get('sub_title') or '')[:12], lecture_dir(it)))

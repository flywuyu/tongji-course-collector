"""Hidden Task Scheduler entry with durable UTF-8 logs and a morning report fallback."""
from pathlib import Path
import sys
import traceback
import time

from pipeline_common import STATE, now, read_json, atomic_text, atomic_json


def main():
    STATE.mkdir(exist_ok=True)
    # Keep the latest 20 operational logs; published course reports are never rotated.
    logs = sorted(STATE.glob('scheduled_*.log'))
    for old in logs[:-19]:
        old.unlink(missing_ok=True)
    log_path = STATE / ('scheduled_' + now().strftime('%Y%m%d_%H%M%S') + '.log')
    with open(log_path, 'a', encoding='utf-8', newline='\r\n', buffering=1) as log:
        sys.stdout = sys.stderr = log
        try:
            import workflow
            if '--run' in sys.argv or '--recover' in sys.argv:
                import weekly_supervisor
                return weekly_supervisor.main(recover='--recover' in sys.argv)
            rc = workflow.main()
            if '--stop' in sys.argv:
                from course_inventory import generate, run_report
                import config as C
                # Let the batch finish its detailed report; do not replace it with an empty stop log.
                deadline=time.monotonic()+60
                while (STATE/'active.json').exists() and time.monotonic()<deadline:
                    time.sleep(1)
                after = generate(deep=True)
                reports=Path(C.DATA)/'运行报告'
                last=read_json(reports/'最新报告.json')
                same_day=str(last.get('written_at',''))[:10]==now().strftime('%Y-%m-%d')
                if same_day and last.get('kind')=='自动更新' and (reports/'最新报告.md').exists():
                    text=(reports/'最新报告.md').read_text(encoding='utf-8')
                    text+='\n\n窗口结束核对：%s，完整讲义 %d/%d。\n' % (now().isoformat(),after['ready'],after['total'])
                    atomic_text(reports/'最新报告.md',text)
                    last['window_end_check']={'at':now().isoformat(),'verified':after['ready'],'total':after['total']}
                    atomic_json(reports/'最新报告.json',last)
                else:
                    run_report({'kind': '窗口结束检查', 'started': now().isoformat(),
                                'result': '本次窗口结束，未发现当天的完整处理报告；清单已更新，剩余由下个补跑窗口处理',
                                'events': []}, after, after)
                if after['ready']<after['total']:
                    workflow.notify_message('课程采集窗口已结束，目前完整讲义%d/%d；剩余%d节已保留，具体原因和补跑安排见课程资料/运行报告。' %
                                            (after['ready'],after['total'],after['total']-after['ready']))
            return rc
        except Exception:
            traceback.print_exc()
            return 1
        finally:
            sys.stdout = sys.__stdout__
            sys.stderr = sys.__stderr__


if __name__ == '__main__':
    raise SystemExit(main())

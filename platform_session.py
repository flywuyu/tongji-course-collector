"""One browser owner for login, schedule discovery and incremental PPT probing."""
import argparse
import json
from pathlib import Path
import time
import sys
import os
from urllib.parse import urlsplit

import config as C
import credentials
from pipeline_common import atomic_json, items, lecture_path, lock, now, read_json,STATE,redact

SITE = 'https://look.tongji.edu.cn/'
AUTH_HOSTS = {'iam.tongji.edu.cn', 'yjlogin.tongji.edu.cn'}


class LoginRequired(RuntimeError):
    pass


def logged_page(ctx):
    for page in ctx.pages:
        if urlsplit(page.url).hostname == 'look.tongji.edu.cn':
            try:
                if page.locator('input[type=password]:visible').count() == 0:
                    return page
            except Exception:
                pass
    return None


def login(ctx, visible=False):
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(SITE, wait_until='domcontentloaded', timeout=60000)
    page.wait_for_timeout(2500)
    try:
        account = credentials.load()
    except (OSError, ValueError):
        account = None
    attempted = False
    expires = time.monotonic() + (300 if visible else 60)
    while time.monotonic() < expires:
        ready = logged_page(ctx)
        if ready:
            ready.wait_for_timeout(1500)
            return ready
        for pg in list(ctx.pages):
            if urlsplit(pg.url).hostname not in AUTH_HOSTS:
                continue
            user = pg.locator('#j_username')
            password = pg.locator('#j_password')
            if not attempted and account and user.is_visible() and password.is_visible():
                attempted = True
                try:
                    user.fill(account['username'])
                    password.fill(account['password'])
                    # Use the active SSO form. Other hidden login forms must not be submitted.
                    form = password.locator('xpath=ancestor::form')
                    button = form.locator('button:visible, input[type=submit]:visible').first
                    if button.count():
                        button.click(timeout=10000)
                    else:
                        password.press('Enter')
                except Exception:
                    raise LoginRequired('自动登录未完成，请打开浏览器手动验证') from None
            if attempted and not visible and pg.locator('#j_checkcode:visible').count():
                raise LoginRequired('统一认证要求验证码，请在周四23:30前双击“提前登录.bat”完成登录')
        page.wait_for_timeout(1000)
    raise LoginRequired('登录未完成；请在周四23:30前双击“提前登录.bat”，完成验证码或二次验证')


def discover(ctx):
    from course_api import DISCOVER_JS as JS
    page = login(ctx)
    # Calendar dates must use Shanghai/local calendar days, not UTC midnight conversion.
    script = JS.replace("const fmt = d => d.toISOString().slice(0, 10);",
                        "const fmt=d=>[d.getFullYear(),String(d.getMonth()+1).padStart(2,'0'),String(d.getDate()).padStart(2,'0')].join('-');")
    script = script.replace("const today = new Date().toISOString().slice(0, 10);",
                            "const today = new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai'}).format(new Date());")
    script = script.replace("while (cur <= endd)", "while (cur <= endd && fmt(cur) <= today)")
    script = script.replace("return await r.text();", "if(!r.ok) throw new Error('api-http-'+r.status); return await r.text();")
    script = script.replace("catch (e) { return ''; }", "catch (e) { throw new Error('api-request-failed'); }")
    script = script.replace("const groups = (tj.result && tj.result.data) || {};",
                            "if(!tj.result) throw new Error('term-api-failed'); const groups=tj.result.data||{};")
    script = script.replace("for (const day of ((j.result && j.result.list) || []))",
                            "if(!j.result || !Array.isArray(j.result.list)) throw new Error('week-api-failed'); for (const day of j.result.list)")
    data = None
    for attempt in range(4):
        data = json.loads(page.evaluate(script))
        if data.get('error') != 'no-token':
            break
        page.reload(wait_until='domcontentloaded')
        page.wait_for_timeout(3000)
    if not data or data.get('error'):
        raise LoginRequired('课程接口凭证不可用，请提前登录；保留原索引')
    if not isinstance(data.get('items'), list) or not data.get('term'):
        raise RuntimeError('发现结果结构异常，保留原索引')
    C.require_initialized()
    old = {str(x['sub_id']): x for x in items()}
    new_ids = []
    for row in data['items']:
        if not all(row.get(k) for k in ('sub_id', 'course_id', 'title', 'date')):
            raise RuntimeError('发现课次字段不完整，保留原索引')
        key = str(row['sub_id'])
        prior = old.get(key, {})
        # Existing directory identity is stable. Signed play URLs and duration may refresh.
        if prior:
            for field in ('title', 'date', 'sub_title'):
                if prior.get(field):
                    row[field] = prior[field]
        else:
            new_ids.append(key)
        old[key] = {**prior, **{k: v for k, v in row.items() if v not in (None, '')}}
    merged = sorted(old.values(), key=lambda x: (x['date'], x.get('start_at') or 0))
    paths = [os.path.normcase(str(lecture_path(x))) for x in merged]
    if len(paths) != len(set(paths)):
        raise RuntimeError('两条课次将写入同一目录，已停止合并，需核对课表')
    if Path(C.CAT).exists():
        atomic_json(C.CAT_BAK, read_json(C.CAT))
    atomic_json(C.WEEKLY, data)
    atomic_json(C.CAT, {'when': now().isoformat(), 'count': len(merged), 'items': merged})
    atomic_json(STATE/'discovery_status.json',{'status':'ok','at':now().isoformat(),
                                              'replays':len(data['items']),'catalog':len(merged)})
    return {'total': len(merged), 'new_ids': new_ids, 'discovered': len(data['items'])}


def probe(ctx, it):
    from ppt_probe import PAT
    page = ctx.new_page()
    found = {}
    status = 'no_uuid'
    owned_pages = [page]
    def on_response(response):
        match = PAT.search(response.url)
        if match:
            found['base'] = match.group(1)
    def on_page(new_page):
        if new_page.opener() in owned_pages:
            owned_pages.append(new_page)
            new_page.on('response', on_response)
    ctx.on('page', on_page)
    try:
        page.on('response', on_response)
        url = SITE + 'livingroom?course_id=%s&sub_id=%s&tenant_code=222' % (it['course_id'], it['sub_id'])
        response = page.goto(url, wait_until='domcontentloaded', timeout=45000)
        if response and response.status in (401, 403, 429):
            return {'status': 'blocked', 'ppt_base': ''}
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and not found:
            # Playwright must pump events; time.sleep alone does not dispatch responses.
            page.wait_for_timeout(500)
            if urlsplit(page.url).hostname in AUTH_HOSTS:
                return {'status': 'login_required', 'ppt_base': ''}
            for pg in list(owned_pages):
                try:
                    for url in pg.evaluate("()=>performance.getEntriesByType('resource').map(e=>e.name)"):
                        m = PAT.search(url)
                        if m:
                            found['base'] = m.group(1)
                            break
                except Exception:
                    pass
        if found:
            status = 'platform'
        elif urlsplit(page.url).hostname != 'look.tongji.edu.cn':
            status = 'probe_error'
        return {'status': status, 'ppt_base': found.get('base', '')}
    except Exception:
        return {'status': 'probe_error', 'ppt_base': ''}
    finally:
        ctx.remove_listener('page', on_page)
        for owned in owned_pages:
            if not owned.is_closed():
                owned.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['check', 'login', 'discover', 'probe'])
    parser.add_argument('--sub-id')
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright
    with lock('browser'), sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(C.PROFILE, headless=args.mode != 'login',
                 args=['--mute-audio'], viewport={'width': 1280, 'height': 900})
        try:
            for stale in ctx.pages[1:]:
                stale.close()
            if args.mode == 'discover':
                result = discover(ctx)
            else:
                page = login(ctx, args.mode == 'login')
                result = {'status': 'logged_in'}
                if args.mode == 'check':
                    # Exercise authenticated APIs too; a home-page URL is insufficient.
                    result.update(discover(ctx))
                if args.mode == 'probe':
                    it = next(x for x in items() if str(x['sub_id']) == args.sub_id)
                    row = {k: it.get(k) for k in ('date', 'title', 'sub_id', 'course_id', 'duration', 'minutes')}
                    row.update(probe(ctx, it), checked_at=now().isoformat())
                    if row['status'] in ('blocked', 'login_required', 'probe_error'):
                        raise RuntimeError('PPT 探测失败，不能当成“没有PPT”：' + row['status'])
                    path = Path(C.PROJ) / 'ppt_清单.json'
                    old = read_json(path)
                    rows = {str(x['sub_id']): x for x in old.get('items', [])}
                    rows[str(it['sub_id'])] = row
                    out = list(rows.values())
                    atomic_json(path, {'when': now().isoformat(), 'total': len(out),
                                      'with_ppt': sum(bool(x.get('ppt_base')) for x in out), 'items': out})
                    result = {'sub_id': it['sub_id'], 'status': row['status']}
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return 0
        finally:
            ctx.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except LoginRequired as e:
        if len(sys.argv)>1 and sys.argv[1] in ('discover','check'):
            atomic_json(STATE/'discovery_status.json',{'status':'failed','at':now().isoformat(),'reason':str(e)})
        print(str(e), flush=True)
        raise SystemExit(20)
    except Exception as e:
        if len(sys.argv)>1 and sys.argv[1] in ('discover','check'):
            atomic_json(STATE/'discovery_status.json',{'status':'failed','at':now().isoformat(),'reason':redact(e)})
        print(redact(e),flush=True)
        raise SystemExit(1)

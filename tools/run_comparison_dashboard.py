"""Fresh local Final/Khaled paper comparison; never uses signed execution.

UI assets come from a pinned, reviewed historical UI commit, while workers import
bot code only from the two explicit current checkouts. Historical bot code is
never extracted. No cloud dashboard credentials or exchange API keys are needed.
"""
import argparse
import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def checked_git(folder, *args):
    result = subprocess.run(['git', '-C', str(folder), *args], capture_output=True,
                            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
    if result.returncode:
        raise RuntimeError(f'Git operation failed in {folder}; check local edits/network before retrying')
    return result.stdout


def validate_source(folder, expected):
    folder = Path(folder).expanduser().resolve()
    remote = checked_git(folder, 'remote', 'get-url', 'origin').decode().strip()
    name = remote.rstrip('/').split('/')[-1].removesuffix('.git')
    if name.lower() != expected.lower():
        raise ValueError(f'{folder} is not the {expected} checkout')
    for filename in ('binance_cascade_hunter_dynamic_v2_paper.py', 'baseline.json', 'requirements.txt'):
        if not (folder / filename).is_file():
            raise ValueError(f'{expected}: missing {filename}')
    cfg = json.loads((folder / 'baseline.json').read_text())
    if cfg.get('exchange', {}).get('mode', 'paper') != 'paper':
        raise ValueError(f'{expected}: exchange.mode must be paper; no live/testnet allowed in comparison')
    if cfg.get('initial_balance_usdt') != 100:
        raise ValueError(f'{expected}: initial_balance_usdt must be 100')
    return folder


def patch_ui_sources(path):
    text = path.read_text()
    old = '"khaled": ROOT,'
    new = '"khaled": Path(os.environ["CASCADE_KHALED_DIR"]).expanduser().resolve(),'
    if text.count(old) != 1:
        raise ValueError('Unexpected comparison UI source layout; refusing to guess')
    path.write_text(text.replace(old, new))


def patch_ui_labels(path):
    """Render each bot's actual exit policy, including the current hard-stop trail."""
    replacements = {
        'function settingsSummary(s)': "function settingsSummary(s){const c=s.settings;if(!c)return 'بانتظار أول لقطة من البوت';const basis=c.position_basis==='current_equity'?'equity الحالي':'الرصيد';const x=c.active_exit?.exit||{};const fixed=x.policy==='hard_stop_trailing';const head=`${fixed?'سقف هامش':'هامش'} ${num(c.MAX_POSITION_PCT*100,1)}% من ${basis} · رافعة ${num(c.LEVERAGE,0)}×`;if(fixed)return `${head} · وقف ${num(x.hard_stop_pct*100,1)}% · تفعيل التريل ${num(x.trail_arm_pct*100,1)}% · مسافته ${num(x.trail_distance_pct*100,1)}%`;return c.active_exit?`${head} · TP/SL حسب ATR · Breakeven + Trailing`:`${head} · وقف ${num(c.STOP_LOSS_PCT*100,1)}% · هدف ${num(c.TAKE_PROFIT_PCT*100,1)}%`}",
        'function settings()': "function settings(){return ids().map(id=>{let s=data(id),c=s.settings||{},a=c.active_exit,t=a?.tp_sl||{},x=a?.exit||{},fixed=x.policy==='hard_stop_trailing';let rows=[[fixed?'سقف هامش الدخول':'هامش الدخول',num(c.MAX_POSITION_PCT*100,1)+'% من '+(c.position_basis==='current_equity'?'equity الحالي':'الرصيد')],['الرافعة',num(c.LEVERAGE,0)+'×'],['حد المراكز',num(c.MAX_CONCURRENT_POSITIONS,0)],...(fixed?[['الوقف من سعر التنفيذ',num(x.hard_stop_pct*100,1)+'%'],['تفعيل التريل',num(x.trail_arm_pct*100,1)+'%'],['مسافة التريل',num(x.trail_distance_pct*100,1)+'% من أفضل سعر'],['جني الربح','عبر التريل؛ لا TP ثابت'],['سقف المخاطرة مع الرسوم',num(t.max_equity_risk_pct*100,2)+'%']]:a?[['الوقف من ATR',num(t.atr_stop_mult,2)+'×ATR مع حدود المخاطرة والتصفية'],['الهدف من ATR',num(t.atr_tp_mult,2)+'×ATR'],['الحد الأدنى R:R',num(t.min_rr,2)],['سقف المخاطرة المخططة',num(t.max_equity_risk_pct*100,2)+'%'],['تفعيل Breakeven',num(x.be_trigger_pct*100,2)+'%'],['مسافة Trailing',num(x.trail_atr_mult,2)+'×ATR']]:[['الوقف السعري',num(c.STOP_LOSS_PCT*100,1)+'%'],['جني الربح',num(c.TAKE_PROFIT_PCT*100,1)+'%']]),['شرط الحركة',num(c.CASCADE_THRESHOLD*100,1)+'%'],['شرط الحجم',num(c.VOLUME_SPIKE,1)+'×'],['رسوم كل تنفيذ',num(s.fee_rate*100,3)+'%']];return `<section class=panel><div class=row><h2>${names[id]} — الإعدادات الفعلية</h2><span class=source>${esc(s.source?.repository)} @ ${esc(s.source?.commit)}</span></div>${table(['الإعداد','القيمة'],rows.map(([k,v])=>[k,esc(v)]))}<details><summary>كامل الإعدادات</summary><pre>${esc(JSON.stringify(c,null,2))}</pre></details><details><summary>سجل التشغيل</summary><pre>${esc(s.logs?.join('\\n')||'لم يصل سجل بعد.')}</pre></details>${exports(id)}</section>`}).join('')}",
    }
    lines = path.read_text().splitlines()
    for prefix, replacement in replacements.items():
        matching = [i for i, line in enumerate(lines) if line.startswith(prefix)]
        if len(matching) != 1:
            raise ValueError('Unexpected comparison UI display layout')
        lines[matching[0]] = replacement
    path.write_text('\n'.join(lines) + '\n')


def extract_ui(repository, revision, destination):
    archive = checked_git(repository, 'archive', revision, 'dashboard')
    destination = Path(destination)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for item in tar.getmembers():
            relative = Path(item.name)
            if relative.is_absolute() or '..' in relative.parts or relative.parts[0] != 'dashboard':
                raise ValueError('Unexpected UI archive path')
            if 'tests' in relative.parts:
                continue
            target = destination / relative
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif item.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tar.extractfile(item).read())
            else:
                raise ValueError('UI archive links are not permitted')
    patch_ui_sources(destination / 'dashboard' / 'paths.py')
    patch_ui_labels(destination / 'dashboard' / 'app.js')


def fresh_environment(runtime_root, final_dir, khaled_dir):
    root = Path(runtime_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    session = Path(tempfile.mkdtemp(prefix='fresh-', dir=root))
    env = dict(os.environ)
    for key in ('PYTHONPATH', 'CASCADE_DASHBOARD_TOKEN', 'BINANCE_API_KEY', 'BINANCE_API_SECRET'):
        env.pop(key, None)
    env.update(CASCADE_RUNTIME_DIR=str(session), CASCADE_FINAL_DIR=str(final_dir),
               CASCADE_KHALED_DIR=str(khaled_dir),
               CASCADE_FINAL_STATE=str(session / 'data' / 'final.state.json'),
               CASCADE_KHALED_STATE=str(session / 'data' / 'khaled.state.json'))
    return session, env


def verify_fresh_accounts(env):
    for name in ('FINAL', 'KHALED'):
        path = Path(env[f'CASCADE_{name}_STATE'])
        state = json.loads(path.read_text())
        if (state['balance_usdt'] != 100 or state['account_positions']
                or state['strategy_positions'] or state['trades']):
            raise ValueError(f'{name}: account is not fresh; refusing to start')
        print(f'{name}: 100 USDT | positions=0 | fills=0', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--final-dir', default='/root/cascade-git')
    parser.add_argument('--khaled-dir', default=str(ROOT))
    parser.add_argument('--prepare-only', action='store_true', help='Prepare/verify fresh states without market connections')
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        raise ValueError('Python 3.11+ required')
    cfg = json.loads((ROOT / 'baseline.json').read_text())['comparison_dashboard']
    if not args.prepare_only:
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try: sock.bind((cfg['host'], cfg['port']))
            except OSError: raise ValueError('Port 8787 is busy. Stop the old local dashboard with Ctrl+C first') from None
    final_dir = validate_source(args.final_dir, 'Final')
    khaled_dir = validate_source(args.khaled_dir, 'Khaled')
    # The caller updates Khaled before invoking this script. Final is updated
    # explicitly from its own remote, never copied or modified by this project.
    if checked_git(final_dir, 'status', '--porcelain', '--untracked-files=no').strip():
        raise ValueError('Final has local tracked edits; preserve/review them before updating')
    print('Updating Final from its own main branch...', flush=True)
    checked_git(final_dir, 'pull', '--ff-only', 'origin', 'main')
    validate_source(final_dir, 'Final')
    ui = ROOT / cfg['ui_cache'] / cfg['ui_commit']
    try:
        checked_git(ROOT, 'cat-file', '-e', cfg['ui_commit'] + ':dashboard/server.py')
    except RuntimeError:
        checked_git(ROOT, 'fetch', '--no-tags', 'origin', cfg['ui_commit'])
    extract_ui(ROOT, cfg['ui_commit'], ui)
    venv = ROOT / cfg['venv_dir']
    python = venv / 'bin' / 'python'
    if not python.is_file():
        subprocess.run([sys.executable, '-m', 'venv', str(venv)], check=True)
    subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(ui / 'dashboard' / 'requirements.txt'),
                    '-r', str(final_dir / 'requirements.txt'), '-r', str(khaled_dir / 'requirements.txt')], check=True)
    session, env = fresh_environment(ROOT / cfg['sessions_dir'], final_dir, khaled_dir)
    print(f'Fresh session: {session}', flush=True)
    print(f'Final source: {final_dir}', flush=True)
    print(f'Khaled source: {khaled_dir}', flush=True)
    subprocess.run([str(python), str(ui / 'dashboard' / 'prepare.py')], cwd=ui, env=env, check=True)
    verify_fresh_accounts(env)
    if args.prepare_only: return
    print(f"Dashboard: http://{cfg['host']}:{cfg['port']} | Ctrl+C stops both workers", flush=True)
    os.chdir(ui)
    os.execve(str(python), [str(python), '-u', str(ui / 'dashboard' / 'server.py')], env)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None

"""tally_io.py — hook 側の入出力。git・ファイル・状態を触るのはここだけ。

判定は tally_core（純関数）に置き、外の世界に触る部分をここへ寄せている。
tally が読むのは次の3か所だけで、それ以外は一切開かない:

  1. <cwd>/tasks.md         （読み取りのみ。書き込みは絶対にしない）
  2. <cwd> の git リポジトリ（git コマンド経由の読み取りのみ）
  3. ~/.claude/tally/       （自分の状態）

他プラグインのファイル（docs/interviews・knowledge・diary など）は見ない。
プラグインは互いの挙動に依存しない、という疎結合の原則による。

pathlib ではなく os.path を使い、re を import しないのは起動時間のため
（実測: python -c pass が 21ms、json/pathlib/re/subprocess/time を入れると 51ms。
SessionStart の目標は 200ms で、git 3本が既に 53〜59ms を占める）。
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tally_core  # noqa: E402

HOME = os.path.expanduser("~")
STATE_ROOT = os.path.join(HOME, ".claude", "tally")
SESSION_DIR = os.path.join(STATE_ROOT, "session")
PROJECT_DIR = os.path.join(STATE_ROOT, "project")

SESSION_TTL = 7 * 24 * 3600       # セッション状態は使い捨て
PROJECT_TTL = 90 * 24 * 3600      # S1 マーカーは消えると1回だけ鳴り直す
GIT_TIMEOUT = 10

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def force_utf8():
    """標準入出力を UTF-8 に固定する。

    Windows では既定が cp932 になり、日本語入りの出力が UTF-8 前提の
    フックリーダーで読めなくなる。この tally を作るときの実測調査スクリプト自身が
    cp932 の UnicodeEncodeError で落ちた。
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def is_disabled():
    """TALLY_DISABLE=1 で全 hook を無音にする（開発中に自分へ鳴らせないため）。"""
    return bool(os.environ.get("TALLY_DISABLE"))


def read_hook_input():
    """stdin の JSON を読む。読めなければ None（＝何もしない側へ倒す）。"""
    try:
        raw = sys.stdin.read()
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def key_for(value):
    """状態ファイル名。長さを抑えた安定キー（tally_core.state_key）にする。

    Windows では大文字小文字と区切り文字の違いを normcase が吸収するので、
    区切り記号と大文字小文字だけが違う同じプロジェクトが、1つの状態を共有する。
    """
    return tally_core.state_key(os.path.normcase(str(value)))


def session_path(session_id):
    return os.path.join(SESSION_DIR, key_for(session_id) + ".json")


def project_path(cwd):
    return os.path.join(PROJECT_DIR, key_for(cwd) + ".json")


def read_json(path):
    """状態を読む。無い・壊れている・読めないは、すべて None。

    外から編集されうるファイルなので、辞書でなければ None に倒す
    （読み込み時に型を確かめないと、あとで .get で落ちる）。
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def write_json(path, data):
    """状態を書く。成功なら True。失敗しても例外は出さず False を返す。

    書けなかったときに促しを出さないのは、連発防止を最優先しているため。
    状態が残らないまま促すと、毎セッション同じ促しが出続ける。
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def cleanup(dirpath, ttl, now=None):
    """TTL を過ぎた状態ファイルを消す。失敗しても無視する。"""
    now = time.time() if now is None else now
    try:
        names = os.listdir(dirpath)
    except OSError:
        return
    for name in names:
        p = os.path.join(dirpath, name)
        try:
            if now - os.stat(p).st_mtime > ttl:
                os.unlink(p)
        except OSError:
            pass


def git(cwd, *args):
    """git を1本走らせ、成功なら stdout を返す。失敗・例外・タイムアウトは None。

    git が入っていない環境でも例外を外へ出さない（絶対条件の「例外は無音」）。
    """
    try:
        p = subprocess.run(
            ["git", "-C", str(cwd)] + list(args),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=GIT_TIMEOUT, creationflags=_NO_WINDOW)
    except Exception:
        return None
    return p.stdout if p.returncode == 0 else None


def _one_hash(out):
    """`--format=%H` の出力を1つのコミットハッシュにする。空・壊れていれば None。"""
    if not out:
        return None
    line = out.strip().splitlines()[0].strip() if out.strip() else ""
    return line or None


def git_head(cwd):
    """HEAD のコミットハッシュ。コミットが無い・リポジトリでないなら None。

    実測（2026-09-10）: 非リポジトリもコミット0のリポジトリも rc=128 を返す。
    リポジトリのサブディレクトリを cwd にすると、そこに .git が無くても rc=0 で
    リポジトリの HEAD が返る。だから「版管理されているか」はこの1本で判定でき、
    .git の存在を見る必要が無い（見ると、サブディレクトリ起動で誤爆する）。
    """
    return _one_hash(git(cwd, "log", "-1", "--format=%H"))


def git_tasks_head(cwd):
    """tasks.md を最後に触ったコミットのハッシュ。未追跡なら None。

    未追跡のとき git は returncode 0 のまま**空文字**を返す（実測: context-lint）。
    「失敗」ではなく「空」なので、rc だけを見ていると空文字を掴む。

    時刻（%ct）ではなくハッシュを返すのは、%ct が秒までしか無く、同じ秒に2つ
    コミットが出ると鮮度の比較が取りこぼすため（単体試験で発見）。
    """
    return _one_hash(git(cwd, "log", "-1", "--format=%H", "--", "tasks.md"))


def git_dirty_count(cwd):
    """未コミットの件数。判定できなければ None。

    --porcelain は未追跡も拾う。雑音になったら -uno へ（実測では --porcelain で
    9/11、-uno で 5/11 が非空）。促しではなく件数の表示にしか使わない。
    """
    out = git(cwd, "status", "--porcelain")
    if out is None:
        return None
    return sum(1 for line in out.splitlines() if line.strip())


def read_tasks(cwd):
    """<cwd>/tasks.md を読む。無い・読めないなら None。読み取り専用。"""
    try:
        with open(os.path.join(cwd, "tasks.md"), "r",
                  encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None

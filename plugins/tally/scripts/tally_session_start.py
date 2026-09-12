"""tally の SessionStart hook — 環境の整備と復元。

stdout に書いたものがそのままセッションの context になる。出すのは2種類:

  促し (S1・S2・S3・S3d・S5) — 直してほしいことを、直せる人に、直せる場で言う
  注入 (S6 ＋ S4 の1行)      — いま何が残っているかを、開始直後に見せる

促しは状態（1回マーカー・連続無視カウンタ）が保存できて初めて連発を防げるので、
状態が書けなかったときは促しだけを落とし、注入は出す。注入は状態を持たないため、
書けない環境でも落とす理由が無い。

matcher は startup|resume|clear（hooks.json 側）。compact を外しているのは、
圧縮のたびに T2 の起点を書き直すと、圧縮前に出たコミットが T2 から消えるため。

例外は握りつぶして無音。tasks.md へは書かない。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tally_core  # noqa: E402
import tally_io as io  # noqa: E402

SOURCES = ("startup", "resume", "clear")


def gather(cwd):
    """判定に要る事実を集める。ここでだけ git とファイルに触る。"""
    facts = {}
    head = io.git_head(cwd)
    facts["has_history"] = head is not None
    facts["head_hash"] = head

    text = io.read_tasks(cwd)
    facts["has_tasks"] = text is not None
    blocks, checked = tally_core.parse_tasks(text or "")
    facts["blocks"] = blocks
    facts["checked"] = checked

    facts["tasks_tracked"] = False
    facts["tasks_commit_ref"] = None
    facts["dirty_count"] = None
    if head:
        if facts["has_tasks"]:
            th = io.git_tasks_head(cwd)
            if th:
                facts["tasks_tracked"] = True
                facts["tasks_commit_ref"] = th
        facts["dirty_count"] = io.git_dirty_count(cwd)
    return facts, head


def remember_start(session_id, cwd, head):
    """T2 の起点（このセッションが始まった時点の HEAD）を残す。

    既に base_head を持つセッションには書き直さない。resume で起点が「いま」へ
    進むと、再開前に出したコミットが T2 から消えるため。
    """
    path = io.session_path(session_id)
    prev = io.read_json(path)
    if isinstance(prev, dict) and prev.get("base_head"):
        return
    io.write_json(path, {
        "base_head": head,
        "nudged": False,
        "cwd": str(cwd),
        "at": time.time(),
    })


def main():
    io.force_utf8()
    if io.is_disabled():
        return 0

    data = io.read_hook_input()
    if data is None:
        return 0
    if data.get("source", "startup") not in SOURCES:
        return 0

    cwd = data.get("cwd")
    if not cwd or not os.path.isdir(cwd):
        return 0

    facts, head = gather(cwd)

    ppath = io.project_path(cwd)
    before = io.read_json(ppath) or {}
    sections, after = tally_core.decide_session(facts, before)

    nudges = [text for kind, text in sections if kind == "nudge"]
    injects = [text for kind, text in sections if kind == "inject"]

    if nudges:
        # 促しは、状態が残せたときだけ出す。残らないまま出すと毎回鳴る。
        after["at"] = time.time()
        if not io.write_json(ppath, after):
            nudges = []
    elif after != before:
        after["at"] = time.time()
        io.write_json(ppath, after)

    session_id = data.get("session_id")
    if session_id:
        remember_start(session_id, cwd, head)

    for text in nudges + injects:
        print(text)
        print()

    io.cleanup(io.SESSION_DIR, io.SESSION_TTL)
    io.cleanup(io.PROJECT_DIR, io.PROJECT_TTL)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # 何が起きても、セッションの開始を妨げない。
        sys.exit(0)

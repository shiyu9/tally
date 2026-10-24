"""tally の Stop hook — 作業した本人に直させる。

このセッションでコミットが出て、tasks.md がそれより古く、未チェック項目が残って
いるなら、セッションの終わりに1回だけ反映を促す。直せる人が、まだその場にいる
うちに言うのが要点で、次のセッションへ回すと誰も文脈を持っていない。

Stop は毎手番走るので、早く諦める順に見る:

  T0 stop_hook_active           → 即終了（公式のループガード。git を1本も叩かない）
  T1 このセッションで促し済み    → 即終了（状態の読み込みだけ）
     起点が無い                 → 即終了（SessionStart が走らなかった経路）
  T2 HEAD が起点と同じ           → 即終了（git 1本だけ）
     tasks.md が古い＋未チェック → 促す（git 2本＋tasks.md の読み込み）

促す場合だけ {"decision": "block", "reason": ...} を stdout に出す。
それ以外は何も出さず exit 0（セッションに一切干渉しない）。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tally_core  # noqa: E402
import tally_io as io  # noqa: E402


def main():
    io.force_utf8()
    if io.is_disabled():
        return 0

    data = io.read_hook_input()
    if data is None:
        return 0

    # T0: 自分の block で再開したターンでは絶対に再発火しない（公式ループガード）
    if data.get("stop_hook_active"):
        return 0

    cwd = data.get("cwd")
    session_id = data.get("session_id")
    if not cwd or not session_id or not os.path.isdir(cwd):
        return 0

    spath = io.session_path(session_id)
    sstate = io.read_json(spath)
    # T1 と「起点が無い」を、git を叩く前に落とす（毎手番走るため）
    if not isinstance(sstate, dict):
        return 0
    if sstate.get("nudged") or not sstate.get("base_head"):
        return 0

    head = io.git_head(cwd)
    facts = {"head_hash": head}
    if not head or head == sstate["base_head"]:
        return 0  # このセッションではコミットが出ていない

    text = io.read_tasks(cwd)
    facts["has_tasks"] = text is not None
    blocks, _ = tally_core.parse_tasks(text or "")
    facts["unchecked_count"] = len(blocks)

    th = io.git_tasks_head(cwd) if facts["has_tasks"] else None
    facts["tasks_tracked"] = th is not None
    facts["tasks_commit_ref"] = th

    if not tally_core.should_nudge_stop(facts, sstate, data.get("stop_hook_active")):
        return 0

    sstate["nudged"] = True
    sstate["at"] = time.time()
    if not io.write_json(spath, sstate):
        # マーカーが残せないなら促さない。残らないまま促すと毎手番鳴る。
        return 0

    reason = tally_core.stop_reason(os.environ.get("CLAUDE_CODE_ENTRYPOINT"))
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # 何が起きても、セッションの終了を妨げない。
        sys.exit(0)

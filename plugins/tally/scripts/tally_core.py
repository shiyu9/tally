"""tally_core.py — 判定部。純関数だけを置く。

ここには副作用が一切無い。git も叩かず、ファイルも開かず、時刻も自分で取らない。
すべて呼び出し側が集めた値を受け取り、「何を出すか」と「状態をどう更新するか」だけを返す。

そうしている理由は撤退条件にある。設計の撤退条件は「hook が Claude Code の更新で
2回以上壊れたら hook を捨てる」で、その検知者は tests/unit/test_tally_core.py である。
検知者が実行環境（git の有無・時計・ホームディレクトリ）に依存すると、壊れたのが
Claude Code なのか手元の環境なのかが判別できなくなる。

import も持たない。SessionStart は毎回走るので、import の分だけ起動が遅くなる
（実測: python -c pass が 21ms、json/pathlib/re/subprocess/time を入れると 51ms）。
文字列の走査は re を使わず str のメソッドで足りる。
"""

# S6: 1回の注入に載せる tasks.md 由来の本文の上限（バイト）。
# 実測（2026-09-10・11プロジェクト）の注入量は 844〜10,718B、中央値 3,706B。
# 6,144B なら 7件が全件通り、4件が打ち切られる。件数ではなくバイトなのは、
# 件数がバイトを予測しないため（あるプロジェクトは 3件で 9,301B、別のは 12件で 1,866B）。
INJECT_BYTE_LIMIT = 6144

# S5: この件数を「超えたら」消し込みを促す。
# 実測の [x] は最大9件で、この閾値では当面鳴らない。relay:record が完了項目を
# 消していたための 0 なので、relay 撤退後に溜まる方向へ変わる。
# 実測根拠のない仮決め（1か月鳴らなければ下げる）。
CHECKED_THRESHOLD = 20

# 同じ促しが、条件の解消されないままこの回数に達したら、以後その項目は黙る。
STREAK_LIMIT = 3

NUDGE_S1 = (
    "tally: このプロジェクトにはまだコミットがありません。未実施なら `git init` して、"
    "最初のコミットを作ってください。tasks.md の鮮度は git のコミット時刻で見るので、"
    "コミットが1つできるまで tally の他の確認は動きません。"
)
NUDGE_S2 = (
    "tally: tasks.md がありません。プロジェクト直下に `tasks.md` を作り、未完の作業を"
    "`- [ ]` の項目で書いてください。理由・手がかり・再開条件は、その項目の下に"
    "字下げした継続行として書くと、次のセッションにそのまま渡ります。"
)
NUDGE_S3 = (
    "tally: tasks.md を最後にコミットしてから、あとで出たコミットがあります。"
    "`git log --oneline {ref}..HEAD` を見て、着手・完了・保留・新規を tasks.md に"
    "反映してください。すでに反映済みなら何もせず進めて構いません。"
)
NUDGE_S3D = (
    "tally: tasks.md が git の管理下にありません。`git add tasks.md` してコミットして"
    "ください。tally は tasks.md の鮮度をコミット時刻で見るため、追跡されるまで"
    "更新の促しは動きません。"
)
NUDGE_S5 = (
    "tally: tasks.md に完了済みの `- [x]` が {n} 件たまっています。済んだ項目は"
    "行ごと消して整理してください。"
)
NUDGE_STOP = (
    "tally: このセッションでコミットが出ましたが、tasks.md はそれより古いままです。"
    "着手・完了・保留・新規を tasks.md に反映してください。新しく保留にしたものは、"
    "理由と再開条件（何が起きたら戻すか）を継続行に書いてください。"
    "すでに反映済みならそのまま終了して構いません。"
)
# 締めの発言の扱いは経路で分ける。モデルには経路が見えないので hook が見分ける。
# 一律に「ヘッドレスなら再掲」と頼むと、対話セッションでも本文を繰り返していた
# （rally の走査で対話 39 件中 6 件・2026-10-24）。
CLOSING_HEADLESS = (
    "反映したあとの締めの発言は、直前にユーザーへ返した本文（質問・手順・要約）を"
    "置き換えないこと。ヘッドレス（`claude -p`）の経路では最後の発言だけが相手に届くため、"
    "要点を再掲するか 1〜2 行の追記に留めてください。"
)
CLOSING_ATTENDED = (
    "反映したあとの締めの発言は、tasks.md を更新したかどうかを 1 行で伝えるだけにしてください。"
    "直前にユーザーへ返した本文（質問・手順・要約）は相手の画面に残っているので、繰り返さないこと。"
)
HEADLESS_ENTRYPOINT = "sdk-cli"  # claude -p で起動したとき（2026-10-24 実測。対話は cli）

SECTION_TITLE = "## tasks.md（未完のタスク {total} 件{dirty}）"
MORE_LINE = "…他 {m} 件は tasks.md を参照"


def _is_heading(line):
    """Markdown の見出し行か。`#` 1〜6 個のあとに空白が続くもの。"""
    n = 0
    for ch in line:
        if ch != "#":
            break
        n += 1
    return 1 <= n <= 6 and len(line) > n and line[n] in " \t"


def _box(line):
    """行がチェックボックス項目なら " " か "x" を返す。違えば None。

    "- [ ] ..." / "* [x] ..." / "  - [X] ..." を拾う。箇条書き記号のあとに
    空白を1つ以上、`[`、1文字、`]` が続く形だけを項目とみなす。
    """
    s = line.lstrip()
    if not s or s[0] not in "-*":
        return None
    i = 1
    while i < len(s) and s[i] in " \t":
        i += 1
    if i == 1:  # 記号の直後に空白が無い（"-[x]" は項目としない）
        return None
    if not s.startswith("[", i) or len(s) < i + 3 or s[i + 2] != "]":
        return None
    mark = s[i + 1]
    if mark == " ":
        return " "
    if mark in "xX":
        return "x"
    return None


def parse_tasks(text):
    """tasks.md の本文から、未チェック項目のブロックと `- [x]` の件数を取り出す。

    ブロックは (直上の見出し または None, その項目の行たち)。項目は次のいずれかが
    来るまで続く: 同じか浅い字下げの行・空行・見出し。継続行にこそ手がかりと理由が
    書かれている（relay の実測で 4〜86 行／全 18〜159 行）ので、1行目だけに
    切り詰めると項目が使えなくなる。

    副作用なし。ファイルは開かない（呼び出し側が読んだ文字列を渡す）。
    """
    if not text:
        return [], 0
    lines = text.splitlines()
    blocks = []
    checked = 0
    heading = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if _is_heading(line):
            heading = line
            i += 1
            continue
        mark = _box(line)
        if mark == "x":
            checked += 1
            i += 1
            continue
        if mark != " ":
            i += 1
            continue
        indent = len(line) - len(line.lstrip())
        start = i
        i += 1
        while i < len(lines):
            nxt = lines[i]
            if not nxt.strip() or _is_heading(nxt):
                break
            if len(nxt) - len(nxt.lstrip()) <= indent:
                break
            i += 1
        blocks.append((heading, lines[start:i]))
    return blocks, checked


def build_injection(blocks, limit=INJECT_BYTE_LIMIT):
    """未チェック項目を上限まで先頭から詰める。返り値は (行たち, 出した件数, 全件数)。

    切るのは必ず項目の境界で、項目を途中で切らない。tasks.md の記述順のまま並べる
    （優先度順にはしない。tally は優先度を知らない）。

    先頭の1件だけで上限を超える場合も、その1件は丸ごと入れる。実測では 105 項目中
    0 件だが（最大 5,321B）、1件も出さずに「他 N 件」だけを返すと、注入の目的
    （開始直後に何が残っているか分かる）がその回だけ完全に失われるため。

    見出しは、直前に出した見出しと違うときだけ添える。見出しのバイトも上限に含める。
    """
    out = []
    used = 0
    shown = 0
    last_heading = None
    for heading, lines in blocks:
        chunk = []
        if heading is not None and heading != last_heading:
            chunk.append(heading)
        chunk.extend(lines)
        # 既に何か出していれば、その前に改行が1バイト入る
        cost = len("\n".join(chunk).encode("utf-8")) + (1 if out else 0)
        if shown > 0 and used + cost > limit:
            break
        out.extend(chunk)
        used += cost
        shown += 1
        if heading is not None:
            last_heading = heading
    return out, shown, len(blocks)


def streak_decide(prev, condition_true):
    """3セッション連続で無視された促しを止める。返り値は (出すか, 次のカウンタ)。

    条件が偽になった＝直された、なので 0 に戻す。真のままなら数え、STREAK_LIMIT に
    達したあとは黙る。促した回数ではなく「条件が真のまま残っている回数」を数えるのは、
    直した直後にまた同じ条件が立った場合にカウンタが進んでしまわないようにするため。
    """
    if not condition_true:
        return False, 0
    if prev >= STREAK_LIMIT:
        return False, prev
    return True, prev + 1


STATE_KEY_LABEL = 48   # 読んで分かるための頭の部分
STATE_KEY_LEN = STATE_KEY_LABEL + 1 + 16


def state_key(text):
    """状態ファイル名にする短くて安定したキーを返す（純関数）。

    長さを必ず {} 文字以下に抑える。cwd をそのまま非英数だけ潰した名前にすると、
    深いパスのプロジェクトで Windows の MAX_PATH（260）を超え、**状態の書き込みが
    黙って失敗して促しが全部消える**（実機プローブで発覚。272 文字だった）。

    頭は人が読んで見当がつくようにパスの末尾から取り、後ろに全体の 64bit FNV-1a を
    付けて衝突を避ける。hashlib を import しないのは起動時間のため。
    """.format(STATE_KEY_LEN)
    text = str(text)
    h = 0xcbf29ce484222325
    for b in text.encode("utf-8", "replace"):
        h ^= b
        h = (h * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
    label = "".join(c if c.isalnum() else "-" for c in text)[-STATE_KEY_LABEL:]
    return "{}-{:016x}".format(label.strip("-") or "x", h)


def new_project_state():
    """プロジェクト状態の初期値。"""
    return {"s1_done": False, "s3d_done": False, "streak": {"S2": 0, "S3": 0, "S5": 0}}


def _streak_of(state, key):
    streak = state.get("streak")
    if not isinstance(streak, dict):
        return 0
    value = streak.get(key)
    return value if isinstance(value, int) and value >= 0 else 0


def decide_session(facts, state):
    """SessionStart で出す節と、更新後のプロジェクト状態を返す。

    facts に要るもの（呼び出し側が集める）:
      has_history     : bool（`git log -1` が成功したか）
      has_tasks       : bool
      tasks_tracked   : bool（git が使えないときは False 扱いでよい）
      head_hash       : str または None（HEAD のコミット）
      tasks_commit_ref: str または None（tasks.md を最後に触ったコミット）
      dirty_count     : int または None
      blocks, checked : parse_tasks の返り値

    返り値: (節のリスト, 新しい state)。節は ("nudge"|"inject", 本文) の対。
    分けているのは、状態が書けなかったときに**促しだけを落として注入は残す**ため。
    促しは状態（1回マーカー・連続カウンタ）が保存できて初めて連発を防げるが、
    注入は状態を持たないので、書けない環境でも目的1（何が残っているか分かる）を
    落とす理由が無い。
    """
    state = dict(state or {})
    base = new_project_state()
    for k, v in base.items():
        state.setdefault(k, v)
    # 状態ファイルは外から編集されうる。辞書でなければ捨てて作り直す
    # （型を確かめずに dict() へ渡すと ValueError で落ちる。単体試験で発見）。
    raw = state.get("streak")
    streak = dict(raw) if isinstance(raw, dict) else {}
    for k in base["streak"]:
        streak.setdefault(k, 0)

    out = []

    # --- S1: コミットがまだ1つも無い ---
    # 判定は `.git` の存在ではなく `git log -1` の成否で行う。実測（2026-09-10）:
    # 非リポジトリもコミット0のリポジトリも rc=128 で、S1 の促し文（git init して
    # 最初のコミットを）はどちらにも当てはまる。一方、リポジトリのサブディレクトリを
    # cwd にすると `.git` は存在しないのに rc=0 になる。`.git` の存在で判定すると、
    # 版管理済みのリポジトリの中で「git init して」と鳴る。この起動の仕方には実績が
    # ある（relay の tasks.md に「リポジトリ内を cwd にして claude を起動した実績」）。
    s1 = not facts.get("has_history")
    if s1:
        if not state["s1_done"]:
            out.append(("nudge", NUDGE_S1))
            state["s1_done"] = True
    else:
        # 版管理されたら、また版管理を捨てたときに1回鳴らせるようにマーカーを戻す。
        state["s1_done"] = False

    # S1 が立っている間は S2・S3・S3d・S4 を評価しない。コミットが1つも無い
    # リポジトリでは tasks.md の鮮度も未コミット件数も判定できないし、S1 が先に
    # 解消されるべき順序でもある。連続無視のカウンタも進めない（評価していない
    # ものを「無視された」と数えると、S1 を直した直後に S2/S3 が黙る）。
    if not s1:
        # --- S2: tasks.md がまだ無い ---
        emit, streak["S2"] = streak_decide(
            _streak_of(state, "S2"), not facts.get("has_tasks"))
        if emit:
            out.append(("nudge", NUDGE_S2))

        if facts.get("has_tasks"):
            if not facts.get("tasks_tracked"):
                # --- S3d: tasks.md はあるが git 未追跡 ---
                # git log -1 -- tasks.md は、未追跡のとき returncode 0 のまま
                # 空文字を返す（実測: context-lint）。無いのではなく空である。
                if not state["s3d_done"]:
                    out.append(("nudge", NUDGE_S3D))
                    state["s3d_done"] = True
            else:
                state["s3d_done"] = False
                # --- S3: tasks.md の最終コミットより後のコミットがある ---
                # mtime では見ない。編集→コミットの順序上、正しく運用しても mtime は
                # 必ず古くなる（rally 実測で15秒）し、clone や pull でも書き換わる。
                #
                # コミット「時刻」でも見ない。%ct は秒までしか無く、同じ秒に2つ
                # コミットが出ると tc == hc になって取りこぼす（単体試験で発見）。
                # 代わりに「tasks.md を最後に触ったコミットが HEAD そのものか」で
                # 見る。git log は HEAD から辿るので、違えば必ず HEAD の方が後。
                tc = facts.get("tasks_commit_ref")
                hh = facts.get("head_hash")
                stale = bool(tc) and bool(hh) and tc != hh
                emit, streak["S3"] = streak_decide(_streak_of(state, "S3"), stale)
                if emit:
                    ref = facts.get("tasks_commit_ref") or "HEAD~1"
                    out.append(("nudge", NUDGE_S3.format(ref=ref[:8])))

    # --- S5: [x] の消し込み（版管理の有無に依らない）---
    checked = facts.get("checked") or 0
    emit, streak["S5"] = streak_decide(
        _streak_of(state, "S5"), checked > CHECKED_THRESHOLD)
    if emit:
        out.append(("nudge", NUDGE_S5.format(n=checked)))

    # --- S6: 未チェック項目の注入（＋S4 の状態表示）---
    # .git が無くても tasks.md があれば注入する。注入は促しではなく復元なので、
    # 版管理の有無とは独立している（実測: 版管理していないのに12項目を抱えた
    # プロジェクトが実在した）。
    blocks = facts.get("blocks") or []
    if blocks:
        lines, shown, total = build_injection(blocks)
        dirty = facts.get("dirty_count")
        # S4 は促さない。件数を1行添えるだけ（実測 9/11 が常時非空で、促すと
        # ほぼ毎回鳴る）。版管理が無いプロジェクトでは件数そのものが無い。
        dirty_part = ""
        if not s1 and isinstance(dirty, int) and dirty > 0:
            dirty_part = " / 未コミット {} 件".format(dirty)
        body = [SECTION_TITLE.format(total=total, dirty=dirty_part), ""]
        body.extend(lines)
        if shown < total:
            body.append("")
            body.append(MORE_LINE.format(m=total - shown))
        out.append(("inject", "\n".join(body)))

    state["streak"] = streak
    return out, state


def stop_reason(entrypoint):
    """CLAUDE_CODE_ENTRYPOINT に応じた Stop の促し文を返す（純関数）。

    見分けられないとき（変数が無い・未知の値）は、重複を生まない対話側の文に倒す。
    """
    closing = CLOSING_HEADLESS if entrypoint == HEADLESS_ENTRYPOINT else CLOSING_ATTENDED
    return NUDGE_STOP + closing


def should_nudge_stop(facts, session_state, stop_hook_active):
    """Stop で促すかを決める（純関数）。

    T0 stop_hook_active / T1 このセッションで促し済み / T2 本体、の順に見る。
    判定に要る材料が1つでも欠けていたら False へ倒す。促さない側の誤りは沈黙で
    済むが、促す側の誤りは毎回のセッション末に無関係な要求が出ることになる。
    """
    if stop_hook_active:
        return False
    if not isinstance(session_state, dict):
        return False
    if session_state.get("nudged"):
        return False
    base = session_state.get("base_head")
    if not base:
        # SessionStart が走らなかった経路（起点が無い）。何と比べればよいか
        # 分からないので黙る。前回のセッションの起点を流用しない。
        return False
    head = facts.get("head_hash")
    if not head or head == base:
        return False  # このセッションではコミットが出ていない
    if not facts.get("has_tasks") or not facts.get("tasks_tracked"):
        return False
    # tasks.md を最後に触ったコミットが HEAD でなければ、そのあとに出たコミットが
    # 反映されていない。コミット時刻ではなく同一性で見る（%ct は秒までしか無く、
    # 同じ秒に2つコミットが出ると取りこぼす）。
    tc = facts.get("tasks_commit_ref")
    if not tc or tc == head:
        return False
    if (facts.get("unchecked_count") or 0) < 1:
        return False
    return True

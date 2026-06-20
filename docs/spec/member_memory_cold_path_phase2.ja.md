# 文書管理システム — Phase 2: コールドパス（使われない記憶の保守）

Phase 1（[member_memory_plan.ja.md](member_memory_plan.ja.md)）の続き。Phase 1 では自己修復をホットパス（タスク中に使った記憶をその場で直す手入れ）だけで成立させた。本書はその穴を埋める **コールドパス** を扱う。Phase 1 実装のノイズにならないよう分離している。

前提として Phase 1 の以下を参照する: `memory` コマンド（§3）、`recent.txt` / MRU と digest（§4）、利用シーンとホットパスの手入れ（§6）、policy と人間承認ゲート（§8）。

## 1. なぜコールドパスが要るか

ホットパスが直せるのは、その run で recall して実際に使った記憶だけ。一度も recall されない記憶は `touch` も `update` もされないまま取り残され、現実とズレていく。これを定期的に拾って直す／退避するのがコールドパス。

> ここでの **使用** = recall で見つけた記憶が今回のタスクに実際に役立ち、エージェントが `memory touch` でそれを示したこと（Phase 1 §6）。recall しただけ（調べただけ）は使用に含まない。使われない記憶 = `touch` されない記憶。

## 2. 仕組み

ホットパスのように既存の run へ相乗りできないので、専用 routine `workflows/memory_maintenance` を `ticket_driven_workflow` と並べ、低頻度・少量で掃引する（実装は §3）。各文書を次の順で処理する:

1. **候補を選ぶ（機械的）**: `updated_at` が古い順・長期間 `touch` されていない・touch ランク下位などで、再訪する少量を取る。
2. **比較対象と突き合わせて判定する**: 判定は、文書をその比較対象と突き合わせて行う。
   - `meta.source` に ticket/PR/thread の URL があれば、member の `issue/pr inspect`・`chat inspect` で現在の状態を取り直し、文書の主張と突き合わせる。解決済み・消滅・記述変更なら `update`（直す）か `archive`（退避）。
   - source が無ければ、同主題の他 memory と突き合わせ、矛盾・重複・上書きを探す。古い方を `update` / `archive`。
   - どちらの比較対象も無い単独文書は内容を判定せず、長期間 `touch` されていなければ未使用として `archive`。
3. **MRU を汚さない**: 掃引は read 系のみで `touch` しない。recency を上げると全記憶が「最近使った」状態になり、コールド検出が壊れる。

突き合わせと採否の判断は LLM（`guildbotics functions/`、§4）。`record`・`promote` は保守ではないので使わない。対象が `kind: policy` 文書のときは `update`/`archive` せず、Phase 1 §8 の方針で変更を提案する（policy の自律更新は禁止）。

## 3. 実装: コードとプロンプト

3点で構成する。いずれも既存の routine 配線（[ticket_driven_workflow.py](../../guildbotics/templates/commands/workflows/ticket_driven_workflow.py) と同型）に倣う:

- 新 `guildbotics/templates/commands/workflows/memory_maintenance.py`: 再訪対象（`updated_at` 古い順を少量）を選び、新 function プロンプトを `context.invoke` する。
- 新 `functions/maintain_memory.ja.md` / `.en.md`: 候補を `get` で読み、§2 の手順で判断する——`meta.source` があれば member の `inspect` 系で再取得して突き合わせ、無ければ同主題の他文書と突き合わせ、それも無ければ未使用退避。結果は `memory update` / `memory archive`。候補が `kind: policy` のときは update/archive せず、Phase 1 §8 の方針で変更を提案する（提案チャネルは下記 §6 の残論点）。read 系のみで `touch` しないことを明記。
- `SimpleEdition.get_default_routines()` に `workflows/memory_maintenance` を追加。掃引間隔は policy front matter の `cold_path_interval_days`（Phase 1 §8 の `load_policy_params()` を拡張）、無ければ控えめな既定。

## 4. LLM 判定の置き場所

コールドパスの意味判定（陳腐化したか・退避すべきか）は `memory` サブコマンドに入れず、`guildbotics functions/` の判定コマンドに置く（自律実行で一貫性が要るため。§3 の `maintain_memory` から呼ぶ）。判定の入力は文書テキスト単体ではなく、文書とその比較対象（再取得した source か他文書、§2）。比較対象が作れない文書は未使用退避にとどめ、内容の真偽判定はホットパス（次にその文書が recall されたとき、Phase 1 §6）へ委ねる。

## 5. コマンド使用例（自律保守）

読むだけなので MRU は不変＝`touch` しない:

```bash
# 1. 新しい順にブラウズして再訪候補を絞る
memory recall --person alice --limit 20
# 2. 怪しい文書の本文を読む
memory get --person alice --id stale-x
# 3. 現実照合 → まだ有効なら直す
memory update --person alice --id stale-x ...
# 4. トピックごと死んでいれば退避
memory archive --person alice --id dead-y
```

## 6. 残論点（詳細設計）

- **`functions/` 陳腐化判定の入出力契約と未使用退避の閾値**。判定3手の方針（§2）は決まったが、判定関数の具体的な入出力（JSON フィールド）は実装インターフェースとしてこれから固める。未使用退避の閾値（未 touch 何か月／touch ランク何位以下）は a-priori に決まらず、チームの稼働量に応じた実測キャリブレーションが要る。
- **policy 変更の提案チャネル**。コールドパス routine は ticket も thread も持たないため、Phase 1 の「提案は ticket コメント / Slack reply」に乗らない。新規 issue 乱立を避けつつどこへ提案するか（既存の提案 issue があれば重複作成しない 等）を決める。

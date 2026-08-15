# Windows で GuildBotics Desktop をビルドする

GuildBotics Desktop は MSVC toolchain による Windows x86_64 ビルドに対応し、ユーザー単位の NSIS installer を生成します。installer の build と smoke test は Windows 実機で行ってください。macOS 上のテストでは移植可能なロジックを確認できますが、Windows Job Object、registry 変更、NSIS 実行は検証できません。

## 前提ツール

repository を clone する前に、以下をインストールします。

- Visual Studio Build Tools 2022 の **Desktop development with C++** workload と Windows SDK。
- [rustup](https://rustup.rs/) の stable `x86_64-pc-windows-msvc` toolchain。
- Node.js 24。
- [uv](https://docs.astral.sh/uv/getting-started/installation/) と Python 3.12 以上。
- Git for Windows。repository の build script は Git Bash から実行します。
- WebView2 Runtime。通常は Windows 11 に含まれます。未導入の場合、NSIS installer は Tauri の download-bootstrapper 方式で導入します。

NSIS は build 時に Tauri が取得します。Windows の bundle target は NSIS のみに限定しているため WiX は不要です。コード署名は今回の対象外であり、生成される installer は未署名です。

## fresh clone の bootstrap

Git Bash で repository root から実行します。

```bash
uv sync --extra test --extra dev
cd desktop
npm ci
cd ..
```

## bundle 前の native test

sidecar build より前に、Windows 固有の Python 挙動と Rust unit test を実行します。

```bash
uv run --no-sync python -m pytest \
  tests/guildbotics/utils/test_processes.py \
  tests/guildbotics/runtime/test_service_control.py \
  tests/guildbotics/runtime/test_service_lock.py \
  tests/guildbotics/cli/test_start_command.py \
  tests/guildbotics/cli/test_stop_command.py \
  tests/guildbotics/cli/test_member_command.py \
  tests/guildbotics/intelligences/agent_runtime/test_environment.py

scripts/desktop-test-rust.sh
```

これらは Windows 上で native 実行してください。特に、GuildBotics process が nested Job Object を作成でき、suspended 状態の AI CLI process を所属させてから resume できることを確認します。
Rust test wrapper はテスト時だけ Tauri の `externalBin` を空にするため、PyInstaller sidecar を build する前でも実行できます。実際の package build では通常の Tauri config が使われ、sidecar は引き続き必須です。

## build

Git Bash で repository root から実行します。

```bash
scripts/desktop-build-backend.sh
scripts/desktop-smoke-sidecars.sh
cd desktop
npm run tauri build -- --bundles nsis
```

PyInstaller executable は以下へ配置されます。

- `desktop/src-tauri/binaries/guildbotics-app-api-x86_64-pc-windows-msvc.exe`
- `desktop/src-tauri/binaries/guildbotics-cli-x86_64-pc-windows-msvc.exe`

NSIS installer は `desktop/src-tauri/target/release/bundle/nsis/` に生成されます。Windows 用 Tauri overlay でも bundle target を NSIS に固定しているため、通常の Windows build が MSI を作ろうとすることはありません。

`tauri dev` では `scripts/desktop-dev-tauri.sh` が実体の PyInstaller `.exe` sidecar を先に build します。frontend の反復を速くする場合は、1 つ目の Git Bash terminal で `scripts/desktop-dev-backend.sh`、別 terminal で Vite dev command を実行し、frontend 変更ごとの sidecar rebuild を避けます。

## install と PATH

初回起動時に Desktop は managed member CLI を `%USERPROFILE%\.guildbotics\bin\guildbotics.exe` へコピーします。NSIS install hook は、同等の entry が存在しない場合だけ `%USERPROFILE%\.guildbotics\bin` を現在のユーザー PATH に追加します。実際に追加した場合だけ所有 marker を記録し、uninstall 時はその marker が示す自分の entry だけを削除します。

bare `guildbotics` を確認するときは、新しい cmd、PowerShell、Git Bash session を開いてください。Windows では system PATH が user PATH より先に評価されるため、system-wide に別の `guildbotics` がある場合はそちらが優先されます。spawned AI CLI process では GuildBotics が managed bin を PATH の先頭へ置くため、この制約はありません。

## smoke checklist

- 生成した NSIS installer で install し、余分な console window を出さずに app が起動する。
- setup wizard を完了して workspace を作成し、期待する実 file が書かれる。
- secret を保存し、Windows 資格情報マネージャーへ格納される。GuildBotics は credential blob を UTF-8 で保存するため、PEM 秘密鍵でも資格情報マネージャーの 2,560 byte 上限を有効に利用できる。
- `%USERPROFILE%\.guildbotics\bin\guildbotics.exe` が存在し、新しい cmd / PowerShell / Git Bash で bare `guildbotics` がそこへ解決される。
- scheduler を start / stop し、二重起動が拒否され、停止要求 file 経由の graceful stop 後に process が残らない。
- command を実行し、Desktop の activity stream に表示される。
- AI CLI を強制停止し、Task Manager 上に子孫 process が残らない。
- 空白・日本語・`$`・backtick を含む複数行 commit message で AI CLI workflow の commit / push を行う。UTF-8 `--content-file` 経路の実地確認となる。
- `to_pdf` を実行し、既存の `PDF conversion requires WeasyPrint native dependencies.` error が表示される。bundle CLI は WeasyPrint を意図的に除外しているため、代替できるのは GTK/Pango/Cairo を導入した通常 Python 環境だけです。
- uninstall する。app、shortcut、installer が所有する PATH entry は消え、`%USERPROFILE%\.guildbotics`、AI CLI skill、workspace、資格情報マネージャーの entry は user data として残る。

native Windows command path では `.sh` custom command をサポートしません。利用には Bash が必要です。Windows ARM64 と Windows code signing も今回の build target 外です。

## 既存環境からの移行

移行元で `guildbotics secrets export` を実行し、安全な経路で出力を移した後、Windows で `guildbotics secrets import` を実行します。設定と memory document が必要なら workspace directory をコピーしてください。memory document は `<workspace>/.guildbotics/state/documents` にあります。

UTF-8 credential adapter 導入前の Windows build は、Python keyring の UTF-16 blob 形式を使っていました。開発中に保存した旧 credential は現在の build とは別の資格情報マネージャー名前空間にあり、読み込まれません。upgrade 後に export file をもう一度 import してください。

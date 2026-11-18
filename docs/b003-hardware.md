# B003 同一Macでの初回統合と実機接続

## 初回統合（機材なし）

同じMacで `Backend → 仮想シリアルのスレーブ → PythonのPTYマスター → B003Lock` を接続する。PTYはPythonが作成する。Node.js 18以上・npm・Python 3.9以上が必要。この構成では `socat`、`pyserial`、`nfcpy`、RC-S330、Atomは不要。

### 自動確認

リポジトリのルートで実行する。Backendの実際の `UsbSerialAdapter` と `SerialBridge` を起動し、隔離したインメモリDBと合成カードを使う。HTTP・LINE・実DBは使用しない。

```sh
npm ci
node scripts/check-b003-pty.js
```

Pythonが別名の環境では `B003_PYTHON=/path/to/python3` を指定する。Mac上で、合成タッチ→Backend生成命令→ACK→解錠成功→貸出中→施錠成功→返却済み、解錠失敗時の承認維持、施錠失敗時の貸出中維持を確認済み。タッチ返信を1回落として同じフレーム・UUIDでの再送と解錠命令1件を確認し、Pythonを再起動して命令を再送し、保存結果の返信と再駆動ゼロも確認した。

### 別ターミナルで手動接続

1. Python側を起動する。初回は新規試験用ディレクトリの台帳パスを使う。同一命令の再起動試験では台帳を再利用する。

   ```sh
   python3 tools/run_b003_lock.py --pty --card-input stdin --mock-drive success \
     --db /tmp/b003-first-test-commands.sqlite
   ```

2. 別ターミナルで、隔離した試験DBを指定してBackendを起動する。`SERIAL_PORT` はPythonが標準出力に表示した値。Pythonを再起動したら表示されたポートでBackendを起動し直す。

   ```sh
   DATABASE_PATH=/path/to/isolated-test.sqlite SERIAL_MODE=usb \
     SERIAL_PORT=/dev/ttysNNN SERIAL_BAUD_RATE=115200 SERIAL_RELAY_ID=RELAY01 npm start
   ```

3. 試験DBにB003を `BIKE03` / `RELAY01` で登録し、合成カード `0123456789ABCDEF` と承認済み試験申請を紐付けておく。Python側の標準入力に `0123456789ABCDEF` を1行入力すると、通常のUSB v1 `card_tap` が送られる。成功時はBackendの返却操作で施錠を試す。失敗試験は新しい試験申請を用意し、`--mock-drive fail-unlock` または `--mock-drive fail-lock` を選ぶ。自動確認はこれらの試験データをケースごとに作成する。

## MVP実機構成（同じMac）

```text
学生証 → RC-S330 ─USB─→ Python（FeliCa読取、B003Lock、永続台帳）
                            ↕ Mac内のPTY（USB v1、BIKE03 / RELAY01）
                          Backend
Python ─USBシリアル─→ Core2（B003専用ファーム） → サーボ・錠
       ← 動作結果 ─── Core2 ← 解錠位置・施錠状態のセンサー
```

1. RC-S330とB003用Core2を同じMacにUSB接続する。PythonはRC-S330を `nfcpy`／`libusb` で開き、Core2の実ポートを駆動ドライバから `pyserial` で専有する。Core2用の接続は115200 baud・8N1・フロー制御なしとする。実ポート名は接続後に確認する。
2. Pythonは `--pty --card-input reader --driver b003_site_driver --db <台帳パス>` で起動する。Backendは上の手動手順と同様にPythonが表示するPTYを開く。RC-S330のタッチはPythonからBackendに送信する。Core2にはカードIDmを送らない。
3. Backendの宛先一致の命令を受けたPythonは台帳に処理開始を記録してACKを返し、Core2に解錠・施錠を依頼する。Core2はセンサーを読み、対応する動作結果をPythonへ返す。ACK、単なるPWM出力、固定待ち時間は成功判定に使わない。
4. Mac–Core2間は操作ID付きの専用JSON Linesを使う方針。ドライバは1操作ずつ送り、同じ操作ID・動作の返答だけを採用する。タイムアウトや切断、位置未確認は失敗扱いとし、駆動指示の自動再送は行わない。Backendの結果タイムアウト10秒以内に結果を返す。Backendの命令再送に対する重複防止はMac側の台帳で継続する。

`b003_site_driver` とB003用Core2ファームは未実装。既存の申請用Core2ファームと `POST /api/requests` に変更はない。駆動・位置検知の実装前に、Core2のモデル、サーボ・錠・電源の型番と定格、使用ピン・駆動範囲、解錠位置と施錠状態を確認するセンサーの型番・配線・入力極性を確定する。

## 検証状況

- 隔離したインメモリDBで仮想シリアルの3ケース（正常・解錠失敗・施錠失敗）を再実行し、すべて成功。Python試験7件も再実行して成功。
- 実カード読取、Core2とのUSB通信、サーボ駆動、物理的な解錠・施錠検知は未検証。

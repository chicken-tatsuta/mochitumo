const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { randomUUID } = require('node:crypto');
const { mkdtempSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const path = require('node:path');
const readline = require('node:readline');
const { once } = require('node:events');
const { openDatabase } = require('../src/db/database');
const { seedDevelopmentData, DEMO_CARD_UID } = require('../src/db/seed');
const { DEMO_DEVICE_ID } = require('../src/development-defaults');
const { createNfcRentalRequest } = require('../src/nfc/request-service');
const { approve, requestLock } = require('../src/rental/service');
const { UsbSerialAdapter } = require('../src/serial/adapters');
const { SerialBridge } = require('../src/serial/bridge');

const syntheticCard = '0123456789ABCDEF';
const python = process.env.B003_PYTHON || 'python3';

async function waitFor(read, description) {
  for (let i = 0; i < 150; i++) {
    const value = read();
    if (value) return value;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error('Timed out waiting for ' + description);
}

async function connect(db, ledger, mode, { dropFirstTapReply = false } = {}) {
  const child = spawn(python, [
    path.join(__dirname, '../tools/run_b003_lock.py'),
    '--pty',
    '--db',
    ledger,
    '--card-input',
    'stdin',
    '--mock-drive',
    mode,
  ]);
  let port;
  let error;
  const diagnostics = [];
  child.on('error', (err) => {
    error = err;
  });
  child.stdin.on('error', (err) => {
    error = err;
  });
  readline.createInterface({ input: child.stdout }).once('line', (line) => {
    port = line;
  });
  readline.createInterface({ input: child.stderr }).on('line', (line) => diagnostics.push(line));
  let bridge;
  async function close() {
    bridge?.close();
    if (child.pid && child.exitCode === null && child.signalCode === null) {
      const exited = once(child, 'exit');
      child.kill('SIGINT');
      await exited;
    }
  }
  try {
    await waitFor(() => {
      if (error) throw error;
      if (child.exitCode !== null) throw new Error(diagnostics.join('\n'));
      return port;
    }, 'Python PTY');
    const adapter = new UsbSerialAdapter({ path: port, baudRate: 115200 });
    const outbound = [];
    const incoming = [];
    let buffer = '';
    adapter.on('data', (data) => {
      buffer += data.toString('utf8');
      const lines = buffer.split('\n');
      buffer = lines.pop();
      incoming.push(...lines.map((line) => JSON.parse(line)));
    });
    const write = adapter.write.bind(adapter);
    adapter.write = (line) => {
      const frame = JSON.parse(line);
      outbound.push(frame);
      if (dropFirstTapReply && frame.message_type === 'card_tap_result') {
        dropFirstTapReply = false;
        return;
      }
      write(line);
    };
    bridge = new SerialBridge(db, adapter, { relayId: 'RELAY01' });
    bridge.start();
    await waitFor(() => bridge.connected, 'Backend serial connection to ' + port);
    return { adapter, child, close, diagnostics, incoming, outbound };
  } catch (err) {
    await close();
    throw err;
  }
}

async function check(mode) {
  const db = openDatabase(':memory:');
  const directory = mkdtempSync(path.join(tmpdir(), 'b003-pty-'));
  const ledger = path.join(directory, 'commands.sqlite');
  let connection;
  try {
    seedDevelopmentData(db);
    db.prepare('UPDATE nfc_cards SET card_uid=? WHERE card_uid=?').run(
      syntheticCard,
      DEMO_CARD_UID,
    );
    db.prepare(
      "UPDATE bikes SET device_id='BIKE03', relay_id='RELAY01' WHERE bike_id='B003'",
    ).run();
    const { response } = createNfcRentalRequest(db, {
      deviceId: DEMO_DEVICE_ID,
      cardUid: syntheticCard,
      action: 'request',
      clientRequestId: randomUUID(),
    });
    const id = response.request_id;
    approve(db, { id, studentId: 'S1002', bikeId: 'B003', eventId: randomUUID() });
    const status = () =>
      db.prepare('SELECT status FROM rental_requests WHERE request_id=?').get(id).status;
    async function assertResult(action, result, requestStatus) {
      const record = await waitFor(
        () =>
          db
            .prepare(
              'SELECT * FROM lock_commands WHERE request_id=? AND action=? AND result_code IS NOT NULL',
            )
            .get(id, action),
        result,
      );
      assert.equal(record.result_code, result);
      assert.ok(record.acked_at);
      assert.equal(status(), requestStatus);
    }

    connection = await connect(db, ledger, mode, { dropFirstTapReply: mode === 'success' });
    connection.child.stdin.write(syntheticCard + '\n');
    if (mode === 'fail-unlock') {
      await assertResult('unlock', 'unlock_failed', 'approved');
    } else {
      await assertResult('unlock', 'unlock_success', 'rented');
      if (mode === 'success') {
        const taps = await waitFor(() => {
          const frames = connection.incoming.filter((frame) => frame.message_type === 'card_tap');
          return frames.length >= 2 && frames;
        }, 'card tap retransmission');
        assert.deepEqual(taps[0], taps[1]);
        assert.equal(
          db
            .prepare(
              "SELECT count(*) AS count FROM lock_commands WHERE request_id=? AND action='unlock'",
            )
            .get(id).count,
          1,
        );
      }
      requestLock(db, { id, studentId: 'S1001', eventId: randomUUID() });
      if (mode === 'fail-lock') {
        await assertResult('lock', 'lock_failed', 'rented');
      } else {
        await assertResult('lock', 'lock_success', 'returned');
        const commands = connection.outbound.filter((frame) => frame.message_type === 'command');
        await connection.close();
        connection = await connect(db, ledger, mode);
        for (const command of commands) {
          connection.adapter.write(JSON.stringify(command) + '\n');
          await waitFor(
            () =>
              connection.incoming.find(
                (frame) =>
                  frame.message_type === 'result' && frame.command_id === command.command_id,
              ),
            'replayed result',
          );
        }
        assert.deepEqual(connection.diagnostics, []);
        assert.deepEqual(
          connection.incoming.map((frame) => frame.message_type),
          ['ack', 'result', 'ack', 'result'],
        );
        assert.deepEqual(
          connection.incoming
            .filter((frame) => frame.message_type === 'result')
            .map((frame) => frame.result),
          ['unlock_success', 'lock_success'],
        );
      }
    }
    console.log(mode + ': passed');
  } finally {
    await connection?.close();
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
}

(async () => {
  for (const mode of ['success', 'fail-unlock', 'fail-lock']) await check(mode);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

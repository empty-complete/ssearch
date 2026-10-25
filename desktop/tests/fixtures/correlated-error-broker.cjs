// Synthetic wire fixture only. It does not establish real P2 authentication/ownership.
let buffer = Buffer.alloc(0);
let context;
let calls = 0;
const send = value => {
  const body = Buffer.from(JSON.stringify(value));
  const header = Buffer.alloc(4); header.writeUInt32BE(body.length);
  process.stdout.write(Buffer.concat([header, body]));
};
process.stdin.on('data', chunk => {
  buffer = Buffer.concat([buffer, chunk]);
  while (buffer.length >= 4 && buffer.length >= 4 + buffer.readUInt32BE(0)) {
    const size = buffer.readUInt32BE(0);
    const frame = JSON.parse(buffer.subarray(4, 4 + size));
    buffer = buffer.subarray(4 + size);
    if (!context) {
      context = frame;
      const ready = { ...frame, kind: 'ready', profile: process.argv[2], key: process.argv[3],
        api_version: '1', service_version: '0.2.0', app_version: '0.2.0',
        generation: '00000000-0000-0000-0000-000000000001',
        installation_id: '00000000-0000-0000-0000-000000000002', owner: true,
        capabilities: ['settings_validate', 'settings_apply', 'start_catalog', 'cancel_job'],
        process: { pid: 1, executable: process.execPath, created: '1' },
        broker: { pid: process.pid, executable: process.execPath, created: '1' } };
      if (process.argv[4] === 'delayed_ready') setTimeout(() => send(ready), 50);
      else send(ready);
    } else {
      calls++;
      const mutation = frame.envelope.data.method !== 'status';
      send({ session_id: frame.session_id, generation: frame.generation,
        envelope: { schema_version: 1, request_id: frame.envelope.request_id,
          data: mutation ? null : { calls },
          error: mutation ? { code: process.argv[4], message: 'synthetic response loss',
            retryable: true, details: { transport: 'fixture' } } : null } });
    }
  }
});
process.stdin.on('end', () => {
  if (process.argv[4] === 'delayed_ready') setTimeout(() => process.exit(0), 150);
  else process.exit(0);
});

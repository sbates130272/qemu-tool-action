// Hands a phase of the action to qemu_tool_action.py. No dependencies, so
// there is nothing to bundle and nothing to install.
const { spawnSync } = require('node:child_process');
const path = require('node:path');

module.exports = function launch(phase) {
  const result = spawnSync(
    'python3',
    [path.join(__dirname, 'qemu_tool_action.py'), phase],
    { stdio: 'inherit', env: { ...process.env, PYTHONUNBUFFERED: '1' } },
  );
  if (result.error) {
    console.log(`::error::qemu-tool-action needs python3 on PATH: ${result.error.message}`);
    process.exit(1);
  }
  process.exit(result.status === null ? 1 : result.status);
};

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, 'patch-extension-csp.py'), 'utf8').replace(/\r\n/g, '\n');
const start = source.indexOf('    function belongsToWindow(');
const end = source.indexOf('\n    // --- журнал', start);
const code = source.slice(start, end);
assert(start >= 0 && end > start);
for (const [platform, paths, current, same, other] of [
  ['darwin', path.posix, '/Users/test/Project A', '/Users/test/Project A', '/Users/test/Project B'],
  ['linux', path.posix, '/home/test/Project A', '/home/test/Project A', '/home/test/Project B'],
  ['win32', path.win32, 'C:\\Users\\Test\\Project A', 'c:/users/test/project a', 'C:\\Users\\Test\\Project B'],
]) {
  const context = vm.createContext({path: paths, process: {platform}, windowId: "this-window",
    vscode: {workspace: {workspaceFolders: [{uri: {fsPath: current}}]}}});
  vm.runInContext(code, context);
  assert(context.belongsToWindow({project: same}), platform);
  assert(!context.belongsToWindow({project: other}), platform);
  assert(!context.belongsToWindow({project: current + '/nested'}), platform);
  assert(!context.belongsToWindow({}), platform);
  assert(context.belongsToWindow({windowId: 'this-window', project: other}), platform);
  assert(!context.belongsToWindow({windowId: 'other-window', project: current}), platform);
  context.vscode.workspace.workspaceFolders = [];
  assert(context.belongsToWindow({windowId: 'this-window'}), platform);
  assert(!context.belongsToWindow({windowId: 'other-window'}), platform);
}
console.log('Restart routing: macOS/Linux/Windows paths and unrelated windows: OK');

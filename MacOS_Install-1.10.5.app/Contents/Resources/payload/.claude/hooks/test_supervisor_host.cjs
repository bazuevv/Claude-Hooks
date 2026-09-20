const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, 'patch-extension-csp.py'), 'utf8').replace(/\r\n/g, '\n');
const begin = source.indexOf('    var windowId = ');
const end = source.indexOf('    function activePanel()', begin);
assert(begin >= 0 && end > begin);
for (const platform of ['darwin', 'linux', 'win32']) {
  const paths = platform === 'win32' ? path.win32 : path.posix;
  const home = platform === 'win32' ? 'C:\\Users\\Тест' : '/home/Тест';
  const root = paths.join(home, '.claude');
  const python = paths.join(home, 'Python runtime', platform === 'win32' ? 'python.exe' : 'python3');
  const project = paths.join(home, 'Project A');
  const data = new Map([[paths.join(root, 'install-state', 'python-path.txt'), python],
    [paths.join(root, 'hooks', 'hook_supervisor.py'), ''], [python, '']]);
  const messages = [], calls = [], handlers = {};
  let folders = [], workspaceChanged;
  const child = {on: (event, handler) => {handlers[event] = handler;}, kill() {}};
  const mockedFS = {mkdirSync() {}, existsSync: name => data.has(name),
    readFileSync: name => {if (!data.has(name)) throw Error('absent'); return data.get(name);},
    writeFileSync: (name, body) => data.set(name, body),
    renameSync: (a, b) => {data.set(b, data.get(a)); data.delete(a);}, unlinkSync: name => data.delete(name)};
  const context = vm.createContext({path: paths, fs: mockedFS, Date, Promise,
    process: {pid: 42, env: {CLAUDE_PROJECT_DIR: 'stale project'}, once() {}},
    vscode: {workspace: {get workspaceFolders() {return folders;},
      onDidChangeWorkspaceFolders: handler => {workspaceChanged = handler;}}},
    panels: [{webview: {postMessage: m => {messages.push(m); return Promise.resolve(true);}}}],
    log() {}, setTimeout() {return 1;}, clearTimeout() {},
    require: name => ({os: {homedir: () => home}, crypto: {randomBytes: () => ({toString: () => 'a'.repeat(32)})},
      child_process: {spawn: (...args) => {calls.push(args); return child;}}})[name]});
  vm.runInContext(source.slice(begin, end), context);
  context.maintainHooks();
  assert.equal(calls.length, 1, platform + ': empty windows start supervisor');
  assert.equal(calls[0][0], python);
  assert.equal(calls[0][2].env.CLAUDE_PROJECT_DIR, undefined);
  assert.equal(calls[0][2].windowsHide, true);
  assert.equal(messages[0].windowId, 'a'.repeat(32));
  context.maintainHooks();
  assert.equal(calls.length, 1, 'no overlapping processes');
  handlers.exit(0);
  folders = [{uri: {fsPath: project}}]; workspaceChanged();
  assert.equal(calls.length, 2);
  assert.equal(calls[1][1][2], '--project');
  assert.equal(calls[1][1][3], project);
  const registration = JSON.parse(data.get(paths.join(root, 'hooks-runtime', 'windows', 'a'.repeat(32) + '.json')));
  assert.deepEqual(registration.projects, [project]);
}
console.log('Supervisor lifecycle: empty windows, workspace changes, Windows/Linux/macOS paths: OK');

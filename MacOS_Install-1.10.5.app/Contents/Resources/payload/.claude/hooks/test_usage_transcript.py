"""Usage follows the requested tab even when its project differs from the server."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("hooks_http", Path(__file__).with_name("http-server.py"))
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class UsageTranscriptTests(unittest.TestCase):
    def test_global_server_requires_explicit_session(self):
        handler = mock.Mock(path="/cache-usage")
        with mock.patch.multiple(server, GLOBAL_INSTALL=True, PROJECT_DIR=""):
            self.assertIsNone(server.Handler._resolve_transcript(handler))
        self.assertEqual(handler._json_response.call_args.args[0], 400)

    def test_restart_project_comes_from_requested_session(self):
        sid = "5fca8a32-40e9-46ec-963c-b0d3dd44634d"
        with tempfile.TemporaryDirectory() as temp:
            project = str(Path(temp) / "second project")
            history = Path(temp) / "history" / server.encode_project_path(project)
            history.mkdir(parents=True)
            (history / (sid + ".jsonl")).write_text(json.dumps({"cwd": project}) + "\n")
            with mock.patch.multiple(server, CLAUDE_PROJECTS_DIR=str(history.parent),
                                     PROJECT_DIR="wrong project"):
                self.assertEqual(server._session_project(sid), project)
                self.assertIsNone(server._session_project("../outside"))
                self.assertIsNone(server._session_project("missing-session"))

    def test_requested_session_outside_server_project(self):
        sid = "5fca8a32-40e9-46ec-963c-b0d3dd44634d"
        with tempfile.TemporaryDirectory() as temp:
            transcript = Path(temp) / "d--Other-Project" / (sid + ".jsonl")
            transcript.parent.mkdir()
            transcript.write_text("", encoding="utf-8")
            handler = mock.Mock(path="/cache-usage?session=" + sid)
            with mock.patch.multiple(server, CLAUDE_PROJECTS_DIR=temp,
                                     PROJECT_DIR="D:/Project/KAYF-LIFE", LOGS_DIR=temp):
                result = server.Handler._resolve_transcript(handler)
            self.assertEqual(result[0], str(transcript))
            handler._json_response.assert_not_called()

    def test_missing_or_invalid_session_never_uses_other_chat(self):
        with tempfile.TemporaryDirectory() as temp:
            for sid, status in [("../outside", 400),
                                ("5fca8a32-40e9-46ec-963c-b0d3dd44634d", 404)]:
                handler = mock.Mock(path="/cache-usage?session=" + sid)
                with mock.patch.multiple(server, CLAUDE_PROJECTS_DIR=temp,
                                         PROJECT_DIR="D:/Project/KAYF-LIFE", LOGS_DIR=temp):
                    self.assertIsNone(server.Handler._resolve_transcript(handler))
                self.assertEqual(handler._json_response.call_args.args[0], status)


if __name__ == "__main__":
    unittest.main()

class RestartWindowTests(unittest.TestCase):
    def test_restart_empty_window_without_transcript(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            (runtime / 'windows').mkdir()
            wid = 'a' * 32
            (runtime / 'windows' / (wid + '.json')).write_text(json.dumps(
                {'windowId': wid, 'ts': time.time() * 1000, 'projects': []}))
            request = runtime / 'request.json'
            handler = mock.Mock()
            handler._read_body.return_value = json.dumps({'windowId': wid}).encode()
            with mock.patch.multiple(server, GLOBAL_INSTALL=True, LOGS_DIR=directory,
                                     RESTART_REQUEST_FILE=str(request)), mock.patch.object(server, '_log'):
                server.Handler._handle_restart_exthost_post(handler)
            self.assertEqual(handler._json_response.call_args.args[0], 200)
            self.assertEqual(json.loads(request.read_text())['windowId'], wid)
            self.assertEqual(json.loads(request.read_text())['project'], '')

    def test_unregistered_window_cannot_restart_random_editor(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = mock.Mock()
            handler._read_body.return_value = json.dumps({'windowId': 'b' * 32}).encode()
            with mock.patch.multiple(server, GLOBAL_INSTALL=True, LOGS_DIR=directory):
                server.Handler._handle_restart_exthost_post(handler)
            self.assertEqual(handler._json_response.call_args.args[0], 400)

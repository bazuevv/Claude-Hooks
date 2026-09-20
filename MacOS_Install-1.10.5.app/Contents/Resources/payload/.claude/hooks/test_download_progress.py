import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import install_dependencies as installer


class DownloadProgressTests(unittest.TestCase):
    def download(self, content, length):
        response = io.BytesIO(content)
        response.headers = {} if length is None else {'Content-Length': str(length)}
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(installer.urllib.request, 'urlopen', return_value=response), \
                mock.patch.dict(os.environ, {'CLAUDE_INSTALL_EVENTS': '1'}), \
                contextlib.redirect_stdout(output):
            file = Path(directory) / 'package'
            installer.download('https://fixture.invalid/package', file, 'node')
            self.assertEqual(file.read_bytes(), content)
        return [json.loads(line.removeprefix('@claude-installer '))
                for line in output.getvalue().splitlines() if line.startswith('@claude-installer ')]

    def test_known_size_has_actual_intermediate_percentages(self):
        events = self.download(b'x' * 1048576, 1048576)
        self.assertEqual([e['percent'] for e in events], [0, 25, 50, 75, 99, 100])

    def test_unknown_length_does_not_invent_percentage(self):
        events = self.download(b'x', None)
        self.assertNotIn('percent', events[0])
        self.assertEqual(events[-1]['percent'], 100)

    def test_marketplace_selects_stable_matching_architecture(self):
        versions = []
        for target, preview in [('win32-x64', False), ('darwin-x64', True), ('darwin-arm64', False), ('darwin-x64', False)]:
            versions.append({'targetPlatform': target, 'properties': [
                {'key': 'Microsoft.VisualStudio.Code.PreRelease', 'value': str(preview).lower()}],
                'files': [{'assetType': 'Microsoft.VisualStudio.Services.VSIXPackage',
                           'source': 'https://fixture.invalid/' + target + str(preview)}]})
        body = json.dumps({'results': [{'extensions': [{'versions': versions}]}]}).encode()
        for target in ('darwin-x64', 'darwin-arm64'):
            with mock.patch.object(installer.urllib.request, 'urlopen', return_value=io.BytesIO(body)):
                self.assertEqual(installer.extension_package_url('fixture.extension', target),
                                 'https://fixture.invalid/' + target + 'False')

    def test_truncated_download_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'Incomplete download'):
            self.download(b'x', 3)


if __name__ == '__main__':
    unittest.main()

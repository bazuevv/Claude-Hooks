import base64
import json
import subprocess
import unittest
from unittest.mock import patch

import image_export


class ImageExportTests(unittest.TestCase):
    def test_save_preserves_bytes_and_generates_unique_name(self):
        data = b"\xff\xd8\xfforiginal JPEG bytes"
        with patch.object(image_export.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '{"ok":true}')) as run:
            result = image_export.export_image({"action": "save", "data_url": "data:image/jpeg;base64," + base64.b64encode(data).decode(), "name": "../../photo.png"})
        self.assertTrue(result["ok"])
        sent = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(base64.b64decode(sent["base64"]), data)
        self.assertRegex(sent["name"], r"^ClaudeCode\(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\)(-\d+)?\.jpg$")
        with patch.object(image_export.time, "time_ns", return_value=1234567890000000000):
            first = image_export.image_filename("png")
            second = image_export.image_filename("png")
        self.assertNotEqual(first, second)
        self.assertIn("-STA", run.call_args.args[0])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_copy_and_cancel(self):
        for action, response in (("copy", {"ok": True}), ("save", {"ok": True, "cancelled": True})):
            with patch.object(image_export.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(response))):
                self.assertEqual(image_export.export_image({"action": action, "data_url": "data:image/png;base64,aW1hZ2U="}), response)

    def test_invalid_requests_do_not_launch_native_action(self):
        with patch.object(image_export.subprocess, "run") as run:
            for payload in (None, {}, {"action": "remove"}, {"action": "copy", "data_url": "file:///x"},
                            {"action": "copy", "data_url": "data:image/jpeg;base64,aW1hZ2U="},
                            {"action": "save", "data_url": "data:image/png;base64,x"}):
                with self.assertRaises(ValueError):
                    image_export.export_image(payload)
            run.assert_not_called()

    def test_native_error_is_reported(self):
        with patch.object(image_export.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "error")):
            with self.assertRaises(RuntimeError):
                image_export.export_image({"action": "copy", "data_url": "data:image/png;base64,aW1hZ2U="})


if __name__ == "__main__":
    unittest.main()

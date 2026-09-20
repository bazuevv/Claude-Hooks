import unittest
from unittest import mock

import audio_player


class AudioPlayerTests(unittest.TestCase):
    def test_mac_uses_native_player_and_duration(self):
        with mock.patch.object(audio_player.sys, "platform", "darwin"), \
                mock.patch.object(audio_player.os, "access", return_value=True):
            self.assertEqual(audio_player.player_command("/a b/звук.mp3", 5),
                             ["/usr/bin/afplay", "-t", "5", "/a b/звук.mp3"])

    def test_windows_and_linux_keep_ffplay(self):
        for platform in ("win32", "linux"):
            with self.subTest(platform=platform), mock.patch.object(audio_player.sys, "platform", platform), \
                    mock.patch.object(audio_player.shutil, "which", return_value="ffplay"):
                self.assertEqual(audio_player.player_command("sound.mp3"),
                                 ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-i", "sound.mp3"])

    def test_missing_player_returns_none(self):
        with mock.patch.object(audio_player.sys, "platform", "linux"), \
                mock.patch.object(audio_player.shutil, "which", return_value=None):
            self.assertIsNone(audio_player.player_command("sound.mp3"))


if __name__ == "__main__":
    unittest.main()

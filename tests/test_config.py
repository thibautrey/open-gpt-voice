import unittest
from unittest.mock import patch

from open_gpt_voice.config import ConfigurationError, Settings


VALID_ENV = {
    "OPENGPT_VOICE_BEARER_TOKEN": "test-token",
    "OPENGPT_VOICE_SILERO_MODEL_PATH": "/models/silero.onnx",
    "OPENGPT_VOICE_WHISPER_MODEL_PATH": "/models/whisper",
    "OPENGPT_VOICE_GLINER_MODEL_PATH": "/models/GLiNER2.5-Decide",
}


class SettingsTests(unittest.TestCase):
    def test_defaults_are_applied(self) -> None:
        settings = Settings.from_env(VALID_ENV)

        self.assertEqual(settings.port, 8000)
        self.assertEqual(settings.vad_pause_ms, 700)
        self.assertEqual(settings.supertonic_voice_map, {"marin": "F1"})
        self.assertEqual(settings.tts_voice, "marin")
        self.assertEqual(settings.router_provider, "gliner")
        self.assertEqual(settings.gliner_task_threshold, 0.7)
        self.assertEqual(settings.delegation_db_path, ".opengpt-voice-delegations.sqlite3")

    def test_tts_voice_is_configurable(self) -> None:
        settings = Settings.from_env({**VALID_ENV, "OPENGPT_VOICE_TTS_VOICE": "ff_siwis"})

        self.assertEqual(settings.tts_voice, "ff_siwis")

    def test_empty_tts_voice_is_rejected(self) -> None:
        environment = {**VALID_ENV, "OPENGPT_VOICE_TTS_VOICE": "  "}

        with self.assertRaisesRegex(ConfigurationError, "OPENGPT_VOICE_TTS_VOICE"):
            Settings.from_env(environment)

    def test_bearer_token_is_optional(self) -> None:
        settings = Settings.from_env({
            "OPENGPT_VOICE_SILERO_MODEL_PATH": "/models/silero.onnx",
            "OPENGPT_VOICE_WHISPER_MODEL_PATH": "/models/whisper",
            "OPENGPT_VOICE_GLINER_MODEL_PATH": "/models/GLiNER2.5-Decide",
        })

        self.assertIsNone(settings.bearer_token)

    def test_missing_required_model_values_are_reported(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "OPENGPT_VOICE_SILERO_MODEL_PATH"):
            Settings.from_env({})

    def test_empty_environment_mapping_does_not_use_process_environment(self) -> None:
        with patch.dict("os.environ", VALID_ENV):
            with self.assertRaisesRegex(ConfigurationError, "OPENGPT_VOICE_SILERO_MODEL_PATH"):
                Settings.from_env({})

    def test_invalid_threshold_is_rejected(self) -> None:
        environment = {**VALID_ENV, "OPENGPT_VOICE_GLINER_TASK_THRESHOLD": "2"}

        with self.assertRaisesRegex(ConfigurationError, "between 0 and 1"):
            Settings.from_env(environment)

    def test_router_provider_is_validated(self) -> None:
        environment = {**VALID_ENV, "OPENGPT_VOICE_ROUTER_PROVIDER": "unknown"}

        with self.assertRaisesRegex(ConfigurationError, "gliner or laya"):
            Settings.from_env(environment)

    def test_laya_provider_requires_laya_paths(self) -> None:
        environment = {
            key: value for key, value in VALID_ENV.items() if key != "OPENGPT_VOICE_GLINER_MODEL_PATH"
        }
        environment.update({
            "OPENGPT_VOICE_ROUTER_PROVIDER": "laya",
            "OPENGPT_VOICE_LAYA_MODEL_PATH": "/models/laya",
            "OPENGPT_VOICE_LAYA_ONNX_PATH": "/models/laya.onnx",
        })

        settings = Settings.from_env(environment)

        self.assertEqual(settings.laya_onnx_path, "/models/laya.onnx")


if __name__ == "__main__":
    unittest.main()

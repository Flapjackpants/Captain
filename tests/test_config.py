import json

import pytest

from captain import config


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "_config_path", lambda: path)
    return path


def test_load_config_default_model(cfg_path):
    assert config.load_config()["whisper_model"] == "large-v3-turbo"


def test_load_config_migrates_old_small_default(cfg_path):
    cfg_path.write_text(json.dumps({"whisper_model": "small"}))
    assert config.load_config()["whisper_model"] == "large-v3-turbo"


def test_load_config_keeps_chosen_small(cfg_path):
    cfg_path.write_text(
        json.dumps({"whisper_model": "small", "whisper_model_chosen": True})
    )
    assert config.load_config()["whisper_model"] == "small"


def test_load_config_keeps_non_default_model(cfg_path):
    cfg_path.write_text(json.dumps({"whisper_model": "medium"}))
    assert config.load_config()["whisper_model"] == "medium"


def test_settings_dialog_model_only_saved_when_changed():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from captain.gui.settings_dialog import SettingsDialog

    app = QApplication.instance() or QApplication([])
    assert app is not None
    cfg = {"whisper_model": "large-v3-turbo"}
    dlg = SettingsDialog(cfg, None)
    vals = dlg.values()
    assert "whisper_model" not in vals
    assert "whisper_model_chosen" not in vals

    dlg.model_combo.setCurrentText("medium")
    vals = dlg.values()
    assert vals["whisper_model"] == "medium"
    assert vals["whisper_model_chosen"] is True

from captain.engine import Transcriber, _repo_for_model

TURBO_REPO = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"


def test_repo_for_turbo_models():
    assert _repo_for_model("large-v3-turbo") == TURBO_REPO
    assert _repo_for_model("turbo") == TURBO_REPO


def test_repo_for_systran_and_custom():
    assert _repo_for_model("small") == "Systran/faster-whisper-small"
    assert _repo_for_model("org/custom") == "org/custom"


def test_transcriber_default_model():
    assert Transcriber().model_name == "large-v3-turbo"

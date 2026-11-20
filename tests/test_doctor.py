from audi28.doctor import doctor_report
from tests.conftest import write_config


def test_missing_key_reports_fix_without_crashing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = write_config(tmp_path)

    report = doctor_report(path, environ={})

    assert report.exit_code == 1
    assert "DEEPSEEK_API_KEY" in report.text
    assert ".env" in report.text
    assert "Traceback" not in report.text


def test_present_key_is_not_printed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = write_config(tmp_path)
    secret = "sk-test-secret-value"

    report = doctor_report(path, environ={"DEEPSEEK_API_KEY": secret})

    assert report.exit_code == 0
    assert secret not in report.text
    assert "DEEPSEEK_API_KEY" in report.text


def test_doctor_reads_dotenv_without_printing_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = write_config(tmp_path)
    secret = "sk-from-dotenv"
    (tmp_path / ".env").write_text(f"DEEPSEEK_API_KEY={secret}\n", encoding="utf-8")

    report = doctor_report(path, environ={})

    assert report.exit_code == 0
    assert secret not in report.text


def test_missing_config_does_not_crash(tmp_path):
    report = doctor_report(tmp_path / "config.toml", environ={})

    assert report.exit_code == 1
    assert "config.toml" in report.text
    assert "Traceback" not in report.text


def test_bad_config_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "config.toml").write_text("[nope]\nx = 1\n", encoding="utf-8")

    report = doctor_report(tmp_path / "config.toml", environ={"DEEPSEEK_API_KEY": "present"})

    assert report.exit_code == 1
    assert "Traceback" not in report.text
    assert "nope" in report.text

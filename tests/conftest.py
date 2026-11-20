from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config.example.toml"


def write_config(directory: Path, *, port: int | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    text = EXAMPLE.read_text(encoding="utf-8")
    if port is not None:
        text = text.replace("port = 8728", f"port = {port}", 1)
    path = directory / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path

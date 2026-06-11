from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)


def resolve(p: str) -> Path:
    """Absolute paths pass through; bare/relative paths land under DATA_DIR."""
    pp = Path(p)
    return pp if pp.is_absolute() else DATA_DIR / pp

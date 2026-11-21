from pathlib import Path

BASE = Path(__file__).parent

def build_system_message() -> str:
    instructions = (BASE / "system_prompt.md").read_text(encoding="utf-8")
    center_info = (BASE / "center_info.md").read_text(encoding="utf-8")
    return instructions + "\n" + center_info
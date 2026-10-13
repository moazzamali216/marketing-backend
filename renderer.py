"""{{placeholder}} extraction and rendering — no template injection surface."""
import re

PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")


def extract_placeholders(*texts: str) -> list[str]:
    """Return unique placeholder keys, in first-seen order."""
    seen: list[str] = []
    for text in texts:
        if not text:
            continue
        for m in PLACEHOLDER_RE.finditer(text):
            key = m.group(1)
            if key not in seen:
                seen.append(key)
    return seen


def render(text: str, row: dict, missing: str = "") -> str:
    """Substitute {{key}} with row[key]; missing keys become `missing`."""
    if not text:
        return ""
    def _sub(m: re.Match) -> str:
        key = m.group(1)
        val = row.get(key)
        if val is None or str(val).strip() == "":
            return missing
        return str(val)
    return PLACEHOLDER_RE.sub(_sub, text)
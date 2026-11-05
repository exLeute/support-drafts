"""Knowledge base: markdown files in kb/, loaded into memory and searched by word overlap."""

import re
from pathlib import Path

from app import settings

KB_DIR = Path(__file__).parent.parent / "kb"


def _stems(text: str) -> set[str]:
    # shortcut: crude stemming by a 4-letter prefix, good enough for ~10 articles; move to pgvector embeddings when the KB grows
    return {w[:4] for w in re.findall(r"\w+", text.lower()) if len(w) > 2}


def _load() -> list[dict]:
    articles = []
    for path in sorted(KB_DIR.glob("*.md")):
        title, _, body = path.read_text().partition("\n")
        title = title.lstrip("# ").strip()
        articles.append({
            "slug": path.stem,
            "title": title,
            "body": body.strip(),
            "url": f"{settings.PUBLIC_BASE_URL}/kb/{path.stem}",
            "title_stems": _stems(title),
            "stems": _stems(body),
        })
    return articles


ARTICLES = _load()
BY_SLUG = {a["slug"]: a for a in ARTICLES}


def search(query: str, limit: int = 3) -> list[dict]:
    q = _stems(query)
    # title matches count double: they say what the article is about
    scored = [(2 * len(q & a["title_stems"]) + len(q & a["stems"]), a) for a in ARTICLES]
    scored.sort(key=lambda s: s[0], reverse=True)
    best = scored[0][0] if scored else 0
    # drop one-word coincidences that ride along with a strong match
    return [a for score, a in scored[:limit] if score > 0 and score * 2 >= best]

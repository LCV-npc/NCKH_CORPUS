from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from models.llm_article import ArticleExtractionResult


class ArticleExportError(RuntimeError):
    pass


class LLMArticleExporter:
    """Export validated LLM metadata alongside the existing corpus files."""

    def export(
        self,
        article: ArticleExtractionResult,
        output_directory: Path,
    ) -> list[dict[str, Any]]:
        try:
            output_directory.mkdir(parents=True, exist_ok=True)
            created: list[dict[str, Any]] = []
            article_path = output_directory / "article.json"
            article_path.write_text(
                article.model_dump_json(indent=2),
                encoding="utf-8",
            )
            created.append(self._record(article_path, "ARTICLE JSON", "article_json", ""))

            if article.keywords:
                keywords_path = output_directory / "keywords.txt"
                content = "; ".join(article.keywords)
                keywords_path.write_text(content, encoding="utf-8")
                created.append(self._record(keywords_path, "Từ khóa", "keywords", content))

            if article.affiliations:
                affiliations_path = output_directory / "affiliations.txt"
                content = "; ".join(article.affiliations)
                affiliations_path.write_text(content, encoding="utf-8")
                created.append(self._record(
                    affiliations_path, "Đơn vị công tác", "affiliations", content,
                ))
            return created
        except OSError as exc:
            raise ArticleExportError("Không thể ghi kết quả LLM xuống filesystem") from exc

    @staticmethod
    def _record(path: Path, section_name: str, label: str, content: str) -> dict[str, Any]:
        return {
            "file_path": str(path),
            "section_name": section_name,
            "heading": section_name,
            "label": label,
            "content_preview": content[:300],
        }

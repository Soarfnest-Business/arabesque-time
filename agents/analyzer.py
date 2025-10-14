import os
import re
from dataclasses import dataclass
from typing import List


@dataclass
class AnalysisResult:
    summary: str
    suggested_area: str
    suggested_targets: List[str]
    questions: List[str]
    risks: List[str]


class Analyzer:
    def __init__(self, repo_root: str):
        self.root = repo_root

    def _read(self, rel: str) -> str:
        try:
            with open(os.path.join(self.root, rel), "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""

    def analyze(self) -> AnalysisResult:
        html_files: List[str] = []
        css_files: List[str] = []
        for root, _dirs, files in os.walk(self.root):
            for fn in files:
                rel = os.path.relpath(os.path.join(root, fn), self.root)
                if rel.startswith("templates/") and rel.endswith(".html"):
                    html_files.append(rel)
                if rel.startswith("static/") and rel.endswith(".css"):
                    css_files.append(rel)

        issues: List[str] = []
        suggestions: List[str] = []
        risks: List[str] = []
        missing_viewport: List[str] = []
        missing_lang: List[str] = []
        inline_styles: List[str] = []
        large_files: List[str] = []

        for f in html_files:
            content = self._read(f)
            if not content:
                continue
            if '<meta name="viewport"' not in content:
                missing_viewport.append(f)
            if not re.search(r"<html[^>]*\blang=", content, re.IGNORECASE):
                missing_lang.append(f)
            if re.search(r"style=\"[^\"]+\"", content):
                inline_styles.append(f)
            if content.count("\n") > 500:
                large_files.append(f)

        if missing_viewport:
            issues.append(f"viewport未設定: {', '.join(missing_viewport[:5])}")
            suggestions.append("_base.html の <head> に viewport を追加")
        if missing_lang:
            issues.append(f"html lang属性なし: {', '.join(missing_lang[:5])}")
            suggestions.append("<html lang=\"ja\"> を明示")
        if inline_styles:
            issues.append(f"インラインstyle検出: {', '.join(inline_styles[:5])}")
            suggestions.append("インラインstyleを static/style.css に移動")
        if large_files:
            issues.append(f"大きいテンプレート: {', '.join(large_files[:3])}")
            suggestions.append("テンプレートの部品化で可読性向上")
        if not suggestions:
            suggestions.append("_base.html と static/style.css の余白とコントラストを微調整")

        targets: List[str] = []
        for f in ["templates/_base.html", "static/style.css", "templates/index.html"]:
            if os.path.exists(os.path.join(self.root, f)):
                targets.append(f)

        questions: List[str] = []
        if missing_viewport:
            questions.append("モバイル最適化を優先しますか？(はい/いいえ)")
        if inline_styles:
            questions.append("インラインスタイル整理を優先しても良いですか？(はい/いいえ)")
        if not questions:
            questions.append("優先する観点は？(アクセシビリティ/可読性/ナビゲーション/パフォーマンス)")

        summary = (
            "現状スキャン: HTML="
            + str(len(html_files))
            + ", CSS="
            + str(len(css_files))
            + "。気づき: "
            + ("; ".join(issues) if issues else "顕著な問題なし")
            + "; 初期提案: "
            + ("; ".join(suggestions))
        )
        return AnalysisResult(
            summary=summary,
            suggested_area="UI/アクセシビリティと可読性",
            suggested_targets=targets[:3],
            questions=questions[:2],
            risks=risks,
        )


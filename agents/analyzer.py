import os
import re
import json
from dataclasses import dataclass
from typing import List, Optional

from .config import AgentConfig


@dataclass
class AnalysisResult:
    summary: str
    suggested_area: str
    suggested_targets: List[str]
    questions: List[str]
    risks: List[str]


class Analyzer:
    def __init__(self, repo_root: str, cfg: Optional[AgentConfig] = None):
        self.root = repo_root
        self.cfg = cfg or AgentConfig()

    def _read(self, rel: str) -> str:
        try:
            with open(os.path.join(self.root, rel), "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""

    def _openai_refine(self, base: AnalysisResult, html_files: List[str], css_files: List[str]) -> Optional[AnalysisResult]:
        """Use OpenAI to refine summary/targets/questions based on repository context."""
        api_key = self.cfg.openai_api_key
        if not api_key:
            return None
        try:
            import openai

            openai.api_key = api_key

            # Prepare small context: list files and tiny snippets
            def snippet(path: str) -> str:
                try:
                    txt = self._read(path)
                    return txt[:600]
                except Exception:
                    return ""

            html_list = html_files[:6]
            css_list = css_files[:3]
            parts = []
            for p in html_list:
                parts.append(f"# {p}\n" + snippet(p))
            for p in css_list:
                parts.append(f"# {p}\n" + snippet(p))
            context = "\n\n".join(parts)

            system = (
                "You are a senior UX/UI reviewer for a Flask+Jinja app. "
                "Propose practical, small, low-risk UI improvements. "
                "Return strict JSON with keys: summary, suggested_area, suggested_targets[], questions[], risks[]."
            )
            user = (
                "Current findings: "
                + base.summary
                + "\nFocus on accessible, non-breaking improvements in templates/*.html and static/*.css only.\n"
                + "Repository context (truncated snippets):\n"
                + context
            )

            resp = openai.chat.completions.create(
                model=self.cfg.openai_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.2,
            )
            txt = resp.choices[0].message.content or "{}"
            data = json.loads(txt)
            summary = data.get("summary") or base.summary
            suggested_area = data.get("suggested_area") or base.suggested_area
            suggested_targets = data.get("suggested_targets") or base.suggested_targets
            questions = data.get("questions") or base.questions
            risks = data.get("risks") or base.risks
            # Keep targets limited and within repo
            filtered_targets = []
            for t in suggested_targets[:5]:
                if isinstance(t, str) and (
                    (t.startswith("templates/") and t.endswith(".html"))
                    or (t.startswith("static/") and t.endswith(".css"))
                ):
                    if os.path.exists(os.path.join(self.root, t)):
                        filtered_targets.append(t)
            if not filtered_targets:
                filtered_targets = base.suggested_targets

            return AnalysisResult(
                summary=summary,
                suggested_area=suggested_area,
                suggested_targets=filtered_targets,
                questions=questions[:3],
                risks=risks,
            )
        except Exception:
            return None

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
        base = AnalysisResult(
            summary=summary,
            suggested_area="UI/アクセシビリティと可読性",
            suggested_targets=targets[:3],
            questions=questions[:2],
            risks=risks,
        )
        # Try OpenAI refinement
        refined = self._openai_refine(base, html_files, css_files)
        return refined or base

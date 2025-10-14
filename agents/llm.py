import logging
from typing import Any, Dict, List, Optional

from .config import AgentConfig


logger = logging.getLogger(__name__)


def _configure_openai(cfg: AgentConfig):
    import openai  # type: ignore

    openai.api_key = cfg.openai_api_key
    # Optional overrides for enterprise/proxy environments
    base_url = getattr(cfg, "openai_base_url", None)
    if base_url:
        try:
            openai.base_url = base_url  # type: ignore[attr-defined]
        except Exception:
            logger.warning("llm.base_url_ignored value=%s", base_url)
    organization = getattr(cfg, "openai_org", None)
    if organization:
        try:
            openai.organization = organization  # type: ignore[attr-defined]
        except Exception:
            logger.warning("llm.organization_ignored value=%s", organization)
    return openai


def chat_completion(
    cfg: AgentConfig,
    messages: List[Dict[str, Any]],
    *,
    model: Optional[str] = None,
    temperature: Optional[float] = 0.2,
) -> Any:
    """
    Wrapper that retries without unsupported params (e.g., temperature for some models).
    Returns the OpenAI response object on success; raises on failure.
    """
    openai = _configure_openai(cfg)
    mdl = model or cfg.openai_model

    def _call(with_temperature: bool):
        if with_temperature and temperature is not None:
            return openai.chat.completions.create(
                model=mdl,
                messages=messages,
                temperature=temperature,
            )
        # omit temperature entirely
        return openai.chat.completions.create(
            model=mdl,
            messages=messages,
        )

    try:
        return _call(True)
    except Exception as e:
        # Retry without temperature if the error suggests unsupported parameter
        msg = str(e)
        if "temperature" in msg and ("unsupported" in msg or "Only the default" in msg):
            logger.info("llm.retry_without_temperature model=%s", mdl)
            return _call(False)
        raise


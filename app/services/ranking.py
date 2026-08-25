from app.config import get_settings

CATEGORY_PRIOR_BOOST = 0.06


def combine_scores(fit_score: float, trust_score: float, n_flags: int) -> tuple[float, float]:
    fit_score = max(0.0, min(1.0, fit_score))
    settings = get_settings()
    rank_score = (
        settings.fit_weight * fit_score
        + settings.trust_weight * trust_score
        - settings.flag_penalty * n_flags
    )
    return round(fit_score, 4), round(max(0.0, rank_score), 4)


def apply_category_priors(raw_fits: list[float], categories: list[str],
                          category_hint: str | None) -> list[float]:
    """Soft prior: nudge candidates toward the user's detected task category."""
    return [
        fit + (CATEGORY_PRIOR_BOOST if category == category_hint else 0.0)
        for fit, category in zip(raw_fits, categories)
    ]


def normalize_scores(scores: list[float]) -> list[float]:
    """Min-max within the retrieved set so fit spread competes fairly with trust spread."""
    if not scores:
        return []
    lo, hi = min(scores), max(scores)
    if hi - lo < 1e-9:
        return [1.0] * len(scores)
    return [(s - lo) / (hi - lo) for s in scores]


FLAG_EXPLANATIONS = {
    "unverified_publisher": "publisher identity is not verified",
    "permission_overreach": "requests more permissions than its stated purpose needs",
    "sensitive_permission_overreach": "overreach includes credentials/filesystem/network access",
    "suspicious_description_imperative": "description contains imperative language aimed at an AI agent",
    "hidden_unicode_characters": "description contains zero-width or bidi control characters",
}


def build_rationale(name: str, fit_score: float, trust_score: float,
                    flags: list[str], category_hint: str | None, category: str) -> str:
    parts = [f"semantic match {fit_score:.2f}"]
    if category_hint and category == category_hint:
        parts.append(f"matches '{category_hint}' intent")
    if trust_score >= 0.85:
        parts.append("strong trust profile")
    elif trust_score >= 0.6:
        parts.append("acceptable trust profile")
    else:
        parts.append("weak trust profile — review before use")
    if flags:
        explained = "; ".join(FLAG_EXPLANATIONS.get(flag, flag) for flag in flags[:3])
        parts.append(f"flagged: {explained}")
    return f"{name}: " + ", ".join(parts)


def rank_candidates(candidates: list[tuple[object, float]], category_hint: str | None) -> list[tuple]:
    """candidates: [(Tool, raw_cosine_fit)] → sorted [(Tool, fit_score, rank_score, rationale)]."""
    if not candidates:
        return []
    settings = get_settings()
    raw_fits = [fit for _, fit in candidates]
    boosted = apply_category_priors(raw_fits, [t.category for t, _ in candidates], category_hint)

    paired = [(c, b) for c, b in zip(candidates, boosted)
              if b >= settings.min_fit_threshold]
    if not paired:
        return []
    candidates, boosted = zip(*paired)

    normalized = normalize_scores(list(boosted))

    scored = []
    for (tool, _), fit_score, rank_score in (
        (cand, *combine_scores(norm, cand[0].trust_score, len(cand[0].trust_flags)))
        for cand, norm in zip(candidates, normalized)
    ):
        rationale = build_rationale(
            tool.name, fit_score, tool.trust_score, tool.trust_flags,
            category_hint, tool.category,
        )
        scored.append((tool, fit_score, rank_score, rationale))
    scored.sort(key=lambda t: t[2], reverse=True)
    return scored

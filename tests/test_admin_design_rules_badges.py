"""Static design rules for the admin status badges (one component, fixed tones, stable contrast).

Each rule is a function over the CSS/JS text so a mutation test can prove that it rejects a violation.
"""

import re
from pathlib import Path

import pytest

ADMIN = Path(__file__).resolve().parents[1] / "app" / "admin" / "static"
CSS_PATH, JS_PATH = ADMIN / "css" / "admin.css", ADMIN / "js" / "admin.js"
BOOTSTRAP = ADMIN / "vendor" / "bootstrap.min.css"
TONES = ("success", "neutral", "warning", "danger")


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _rules(css: str) -> list[tuple[str, str]]:
    return [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+){([^}]*)}", _strip_comments(css))]


def _line(text: str, needle: str) -> int:
    return text[: max(text.index(needle), 0)].count("\n") + 1 if needle in text else 0


def rule_tones_use_tokens_only(css: str) -> list[str]:
    errors = []
    rules = dict(_rules(css))
    for tone in TONES:
        body = rules.get(f".status-badge-{tone}")
        if body is None or "--status-badge-fg" not in body or "--status-badge-bg" not in body:
            errors.append(f"tone .status-badge-{tone} is not defined with --status-badge-fg/-bg; define it in admin.css")
        elif re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", body):
            errors.append(f".status-badge-{tone} hardcodes a colour (admin.css:{_line(css, f'.status-badge-{tone}')}); use a --bs-* variable")
    return errors


def rule_no_badge_specific_metrics(css: str) -> list[str]:
    errors = []
    metrics = re.compile(r"(?:^|;)\s*(?:font-|padding|height|border-radius|line-height)")
    for selector, body in _rules(css):
        if "status-badge" in selector and selector != ".status-badge" and "::before" not in selector and metrics.search(body):
            errors.append(f"{selector} overrides size/typography (admin.css:{_line(css, selector)}); only .status-badge may set it")
    return errors


def rule_badge_keeps_dot_and_text(css: str, js: str) -> list[str]:
    errors = []
    dot = dict(_rules(css)).get(".status-badge::before", "")
    if not re.search(r"content:\s*''", dot) or not re.search(r"width:", dot) or not re.search(r"height:", dot):
        errors.append("the status dot (.status-badge::before) is missing; it is the non-colour cue, keep it")
    if re.search(r"\.status-badge[^{]*{[^}]*(?:font-size:\s*0|color:\s*transparent|display:\s*none)", _strip_comments(css)):
        errors.append("a status badge hides its text; the status text must stay visible")
    return errors


def rule_flow_badges_never_dimmed(css: str, js: str) -> list[str]:
    errors = []
    for selector, body in _rules(css):
        if "flow-badge" in selector and re.search(r"opacity\s*:\s*(?!1\b)|transition[^;]*opacity", body):
            errors.append(f"{selector} dims or fades a flow badge (admin.css:{_line(css, selector)}); remove opacity, it breaks the contrast")
    if re.search(r"flow-badge[^{]*is-stale", css):
        errors.append("a .flow-badge .is-stale rule exists; stale readings must not dim the badges")
    return errors


def rule_flow_badge_classes_set_in_one_place(js: str) -> list[str]:
    errors = []
    updates = [m.start() for m in re.finditer(r"setClass\(badge\.(?:state|node),", js)]
    body_start = js.find("function setBadge(")
    body = js[body_start: js.find("\n    }\n", body_start)] if body_start >= 0 else ""
    if not body or any(not (body_start <= pos < body_start + len(body)) for pos in updates):
        errors.append("flow badge classes must be toggled only inside setBadge() (admin.js); a second writer causes flicker")
    if "is-stale" in body:
        errors.append(f"setBadge() sets is-stale (admin.js:{_line(js, 'function setBadge(')}); remove the dimming state")
    return errors


def _tokens(name: str) -> tuple[str, str]:
    values = re.findall(rf"--bs-{name}:\s*([^;}}]+)", BOOTSTRAP.read_text(encoding="utf-8"))
    return values[0].strip(), values[1].strip()


def _rgba(value: str) -> tuple[float, float, float, float]:
    if value.startswith("#"):
        return (*(int(value[i:i + 2], 16) for i in (1, 3, 5)), 1.0)
    numbers = [float(n) for n in re.findall(r"[\d.]+", value)]
    return (*numbers[:3], numbers[3] if len(numbers) > 3 else 1.0)


def _luminance(rgb) -> float:
    lin = [(c / 255 / 12.92) if c / 255 <= 0.03928 else ((c / 255 + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(fg: str, bg: str) -> float:
    r, g, b, a = _rgba(fg)
    back = _rgba(bg)[:3]
    mixed = tuple(c * a + k * (1 - a) for c, k in zip((r, g, b), back, strict=True))
    hi, lo = sorted((_luminance(mixed), _luminance(back)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _tone_tokens(css: str, tone: str) -> tuple[str, str]:
    body = dict(_rules(css))[f".status-badge-{tone}"]
    fg = re.search(r"--status-badge-fg:\s*var\(--bs-([\w-]+)\)", body)[1]
    bg = re.search(r"--status-badge-bg:\s*var\(--bs-([\w-]+)\)", body)[1]
    return fg, bg


def rule_tone_contrast(css: str) -> list[str]:
    errors = []
    for tone in TONES:
        fg, bg = _tone_tokens(css, tone)
        for theme, idx in (("light", 0), ("dark", 1)):
            ratio = contrast(_tokens(fg)[idx], _tokens(bg)[idx])
            if ratio < 4.5:
                errors.append(f".status-badge-{tone} ({theme}) has {ratio:.2f}:1 text contrast, needs 4.5:1; pick other --bs-* tokens")
    return errors


CSS = CSS_PATH.read_text(encoding="utf-8")
JS = JS_PATH.read_text(encoding="utf-8")


def test_tones_are_defined_and_use_design_tokens_only():
    assert not rule_tones_use_tokens_only(CSS)


def test_badges_have_no_badge_specific_size_or_typography_overrides():
    assert not rule_no_badge_specific_metrics(CSS)


def test_badges_keep_the_dot_and_the_status_text():
    assert not rule_badge_keeps_dot_and_text(CSS, JS)


def test_flow_badges_are_never_dimmed_or_faded():
    assert not rule_flow_badges_never_dimmed(CSS, JS)


def test_flow_badge_classes_are_written_in_one_place():
    assert not rule_flow_badge_classes_set_in_one_place(JS)


def test_every_tone_meets_aa_text_contrast_in_light_and_dark():
    assert not rule_tone_contrast(CSS)


@pytest.mark.parametrize(
    ("rule", "args"),
    [
        (rule_tones_use_tokens_only, (".status-badge-success { --status-badge-fg: #198754; --status-badge-bg: var(--x); }",)),
        (rule_no_badge_specific_metrics, (".status-badge-warning { --status-badge-fg: red; padding: 1px; }",)),
        (rule_badge_keeps_dot_and_text, ("", "")),
        (rule_flow_badges_never_dimmed, (".flow-badge.is-stale { opacity: .55; }", "")),
        (rule_flow_badge_classes_set_in_one_place, ("function setBadge(a) {\n    }\n  setClass(badge.node, 'x', 1)",)),
    ],
)
def test_each_rule_rejects_a_violating_example(rule, args):
    assert rule(*args)


def test_contrast_helper_rejects_a_pale_pair():
    assert contrast("#cccccc", "#ffffff") < 4.5 <= contrast("#000000", "#ffffff")

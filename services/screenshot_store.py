from models import db, BiliScreenshotTemplate
from services.screenshot_templates import DEFAULT_HTML_TEMPLATES


def get_screenshot_templates(binding_id: int) -> BiliScreenshotTemplate:
    if not binding_id:
        return BiliScreenshotTemplate(
            binding_id=0,
            template_dynamic=DEFAULT_HTML_TEMPLATES.get("dynamic", ""),
            template_live=DEFAULT_HTML_TEMPLATES.get("live", ""),
        )
    template = BiliScreenshotTemplate.query.get(binding_id)
    if template:
        return template
    template = BiliScreenshotTemplate(
        binding_id=binding_id,
        template_dynamic=DEFAULT_HTML_TEMPLATES.get("dynamic", ""),
        template_live=DEFAULT_HTML_TEMPLATES.get("live", ""),
    )
    db.session.add(template)
    db.session.commit()
    return template


def get_screenshot_template_value(binding_id: int, key: str) -> str:
    template = get_screenshot_templates(binding_id)
    value = ""
    if key == "dynamic":
        value = template.template_dynamic
    elif key == "live":
        value = template.template_live
    if value:
        return value
    return DEFAULT_HTML_TEMPLATES.get(key, "")


def save_screenshot_templates(binding_id: int, template_dynamic: str, template_live: str):
    if not binding_id:
        return
    template = BiliScreenshotTemplate.query.get(binding_id)
    if not template:
        template = BiliScreenshotTemplate(binding_id=binding_id)
        db.session.add(template)
    template.template_dynamic = template_dynamic or ""
    template.template_live = template_live or ""
    db.session.commit()


def delete_screenshot_templates(binding_id: int):
    if not binding_id:
        return
    template = BiliScreenshotTemplate.query.get(binding_id)
    if template:
        db.session.delete(template)
        db.session.commit()


def ensure_screenshot_templates(binding_id: int, template_dynamic: str, template_live: str):
    if not binding_id:
        return
    template = BiliScreenshotTemplate.query.get(binding_id)
    if template:
        return
    template = BiliScreenshotTemplate(
        binding_id=binding_id,
        template_dynamic=template_dynamic or DEFAULT_HTML_TEMPLATES.get("dynamic", ""),
        template_live=template_live or DEFAULT_HTML_TEMPLATES.get("live", ""),
    )
    db.session.add(template)
    db.session.commit()

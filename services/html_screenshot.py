import logging
import os

from config import SCREENSHOT_TEMPLATE_PATH, SCREENSHOT_WAIT

_LOGGER = logging.getLogger("html_screenshot")


def render_html_to_image(html: str) -> bytes | None:
    if not html:
        return None
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _LOGGER.info("Playwright not available: %s", exc)
        return None

    template_path = SCREENSHOT_TEMPLATE_PATH
    if template_path and not os.path.exists(template_path):
        template_path = None

    with sync_playwright() as p:
        browser = None
        context = None
        page = None
        try:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 720})
            page = context.new_page()
            # Bound per-operation waits to reduce long-hanging renderer tasks.
            page.set_default_timeout(15000)
            page.set_default_navigation_timeout(15000)

            if template_path:
                page.goto(f"file://{template_path}")
            page.set_content(html, wait_until="load")
            page.wait_for_load_state("networkidle")
            if SCREENSHOT_WAIT:
                page.wait_for_timeout(int(SCREENSHOT_WAIT * 1000))
            try:
                page.evaluate(
                    """() => {
                    const imgs = Array.from(document.images || []);
                    for (const img of imgs) {
                      img.loading = "eager";
                      img.decoding = "sync";
                    }
                }"""
                )
                page.evaluate(
                    """async () => {
                    const fontReady = document.fonts && document.fonts.ready ? document.fonts.ready : null;
                    if (fontReady) {
                      try { await fontReady; } catch (e) {}
                    }
                    const imgs = Array.from(document.images || []);
                    const waitForImg = (img) => new Promise((resolve) => {
                      if (img.complete) return resolve();
                      const done = () => resolve();
                      img.addEventListener("load", done, { once: true });
                      img.addEventListener("error", done, { once: true });
                    });
                    await Promise.race([
                      Promise.all(imgs.map(waitForImg)),
                      new Promise((resolve) => setTimeout(resolve, 3000))
                    ]);
                }"""
                )
            except Exception:
                pass

            target = page.locator(".card, #capture-root, body > *").first
            box = None
            try:
                if target and target.count() > 0:
                    box = target.bounding_box()
            except Exception:
                box = None
            if box and box.get("width") and box.get("height"):
                clip_width = max(1, int(box["x"] + box["width"]))
                clip_height = max(1, int(box["y"] + box["height"]))
                try:
                    page.set_viewport_size({"width": clip_width, "height": clip_height})
                except Exception:
                    pass
                clip = {
                    "x": box["x"],
                    "y": box["y"],
                    "width": box["width"],
                    "height": box["height"],
                }
                return page.screenshot(type="png", clip=clip)

            dims = page.evaluate(
                """() => {
                const doc = document.documentElement;
                const body = document.body;
                const width = Math.max(body ? body.scrollWidth : 0, doc.scrollWidth, doc.clientWidth);
                const height = Math.max(body ? body.scrollHeight : 0, doc.scrollHeight, doc.clientHeight);
                return { width, height };
            }"""
            )
            if isinstance(dims, dict) and dims.get("width") and dims.get("height"):
                clip_width = max(1, int(dims["width"]))
                clip_height = max(1, int(dims["height"]))
                try:
                    page.set_viewport_size({"width": clip_width, "height": clip_height})
                except Exception:
                    pass
                clip = {
                    "x": 0,
                    "y": 0,
                    "width": clip_width,
                    "height": clip_height,
                }
                return page.screenshot(type="png", clip=clip)

            element = page.locator("html")
            box = element.bounding_box()
            if box:
                clip = {
                    "x": box["x"],
                    "y": box["y"],
                    "width": box["width"],
                    "height": box["height"],
                }
                return page.screenshot(type="png", clip=clip)
            return page.screenshot(type="png", full_page=True)
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
